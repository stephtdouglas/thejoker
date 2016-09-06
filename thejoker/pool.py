# -*- coding: utf-8 -*-
"""
Original author: Rodrigo Luger

Included in this project and licensed under MIT with his permission.

Implementations of four different types of processing pools:

    - MPIPool: An MPI pool borrowed from ``emcee``. This pool passes Python
      objects back and forth to the workers and communicates once per task.

    - MPIOptimizedPool: An attempt at an optimized version of the MPI pool,
      specifically for passing arrays of numpy floats. If the length of the
      array passed to the ``map`` method is larger than the number of processes,
      the iterable is passed in chunks, which are processed *serially* on each
      processor. This minimizes back-and-forth communication and should increase
      the speed a bit.

    - MultiPool: A multiprocessing for local parallelization, borrowed from
      ``emcee``

    - SerialPool: A serial pool, which uses the built-in ``map`` function

"""

from __future__ import division, print_function, absolute_import, unicode_literals

import logging
VERBOSE = 5
logging.addLevelName(VERBOSE, "VERBOSE")

import numpy as np
import sys
try:
    from mpi4py import MPI
except ImportError:
    MPI = None
import signal
import functools
import multiprocessing
import multiprocessing.pool
from astropy import log

__all__ = ['MPIPool', 'MPIPool2', 'MPIOptimizedPool', 'MultiPool',
           'SerialPool', 'choose_pool']

# Tags and messages
TAG_TASK = 0
TAG_NEW_FUNC = 1
TAG_NEW_DIMS = 2
TAG_CLOSE = 3
MSG_EMPTY = lambda: np.empty(1, dtype='float64')

class _close_pool_message(object):
    def __repr__(self):
        return "<Close pool message>"

class _function_wrapper(object):
    def __init__(self, function):
        self.function = function

def _placeholder_function(*args):
    """
    The placeholder worker function. Should be replaced
    with the desired mapping function on the first
    call.
    """
    raise Exception("Pool was sent tasks before being told what function to apply.")

class GenericPool(object):
    """ A generic multiprocessing pool object with a ``map`` method. """

    def __init__(self, **kwargs):
        self.rank = 0

    @staticmethod
    def enabled():
        return False

    def is_master(self):
        return self.rank == 0

    def is_worker(self):
        return self.rank != 0

    def wait(self):
        return NotImplementedError('Method ``wait`` must be called from subclasses.')

    def map(self, *args, **kwargs):
        return NotImplementedError('Method ``map`` must be called from subclasses.')

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

class MPIPool(GenericPool):
    """
    A pool that distributes tasks over a set of MPI processes. MPI is an API for
    distributed memory parallelism.  This pool will let you run processes
    without shared memory, letting you use much larger machines.

    The pool only supports the :func:`map` method at the moment. That being
    said, this pool is fairly general and it could be used for other purposes.

    :param comm: (optional)
        The ``mpi4py`` communicator.

    :param loadbalance: (optional)
        if ``True`` and ntask > Ncpus, tries to loadbalance by sending
        out one task to each cpu first and then sending out the rest
        as the cpus get done.
    """
    def __init__(self, comm=None, loadbalance=True,
                 wait_on_start=True, exit_on_end=True, **kwargs):

        if MPI is None:
            raise ImportError("Please install mpi4py")

        self.comm = MPI.COMM_WORLD if comm is None else comm
        self.rank = self.comm.Get_rank()
        self.size = self.comm.Get_size() - 1
        self.function = _placeholder_function
        self.loadbalance = loadbalance

        if self.size == 0:
            raise ValueError("Tried to create an MPI pool, but there "
                             "was only one MPI process available. "
                             "Need at least two.")

        self.exit_on_end = exit_on_end

        # Enter main loop for workers?
        if wait_on_start:
            if self.is_worker():
                self.wait()

    @staticmethod
    def enabled():
        if MPI is not None:
            if MPI.COMM_WORLD.size > 1:
                return True
        return False

    def wait(self):
        """ If this isn't the master process, wait for instructions. """

        if self.is_master():
            raise RuntimeError("Master node told to await jobs.")

        status = MPI.Status()

        # The main event loop:
        while True:
            # Sit and await instructions
            log.log(VERBOSE, "Worker {0} waiting for task.".format(self.rank))

            # Blocking receive to wait for instructions.
            task = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)

            log.log(VERBOSE, "Worker {0} got task {1} with tag {2}."
                    .format(self.rank, type(task), status.tag))

            # Check if message is special sentinel signaling end; if so, stop
            if isinstance(task, _close_pool_message):
                log.log(VERBOSE, "Worker {0} told to quit.".format(self.rank))
                break

            # Check if message is special type containing new function
            #   to be applied
            if isinstance(task, _function_wrapper):
                self.function = task.function
                log.log(VERBOSE, "Worker {0} replaced its task function: {1}."
                        .format(self.rank, self.function))
                continue

            # If not a special message, just run the known function on
            #   the input and return it asynchronously.
            result = self.function(task)
            log.log(VERBOSE, "Worker {0} sending answer {1} with tag {2}."
                    .format(self.rank, type(result), status.tag))
            self.comm.isend(result, dest=0, tag=status.tag) # send to master

        # kill the process if exit on end
        if self.exit_on_end:
            sys.exit(0)

    def map(self, function, tasks):
        """
        Like the built-in :func:`map` function, apply a function to all
        of the values in a list and return the list of results.

        :param function:
            The function to apply to the list.

        :param tasks:
            The list of elements.

        """
        n_tasks = len(tasks)

        # If not the master just wait for instructions.
        if not self.is_master():
            self.wait()
            return

        # Replace the function to apply with the input
        if function is not self.function:
            log.log(VERBOSE, "Master replacing pool function with {0}."
                    .format(function))

            self.function = function
            F = _function_wrapper(function)

            # Tell all the workers what function to use.
            requests = []
            for i in range(self.size):
                r = self.comm.isend(F, dest=i + 1)
                requests.append(r)

            # Wait until all of the workers have responded. See:
            #       https://gist.github.com/4176241
            MPI.Request.waitall(requests)

        if (not self.loadbalance) or (n_tasks <= self.size):

            # Send all the tasks off and wait for them to be received:
            requests = []
            for i, task in enumerate(tasks):
                worker = i % self.size + 1
                log.log(VERBOSE, "Sent task {0} to worker {1} with tag {2}."
                        .format(type(task), worker, i))
                r = self.comm.isend(task, dest=worker, tag=i)
                requests.append(r)

            MPI.Request.waitall(requests)

            # Receive the responses:
            results = []
            for i in range(n_tasks):
                worker = i % self.size + 1
                log.log(VERBOSE, "Master waiting for worker {0} with tag {1}"
                        .format(worker, i))
                result = self.comm.recv(source=worker, tag=i)

                results.append(result)

            return results

        else:

            # Perform load-balancing. The order of the results are likely to
            #   be different from the case with no load-balancing
            for i, task in enumerate(tasks[0:self.size]):
                worker = i+1
                log.log(VERBOSE, "Sent task {0} to worker {1} with tag {2}."
                        .format(type(task), worker, i))

                # Send out the tasks asynchronously.
                self.comm.isend(task, dest=worker, tag=i)

            ntasks_dispatched = self.size
            results = [None]*n_tasks
            for itask in range(n_tasks):
                status = MPI.Status()

                # Receive input from workers.
                try:
                    result = self.comm.recv(source=MPI.ANY_SOURCE,
                                            tag=MPI.ANY_TAG, status=status)
                except Exception as e:
                    self.close()
                    raise e

                worker = status.source
                i = status.tag
                results[i] = result

                log.log(VERBOSE, "Master received from worker {0} with tag {1}"
                        .format(worker, i))

                # Send the next task to this idle worker (if there are any left)
                if ntasks_dispatched < n_tasks:
                    task = tasks[ntasks_dispatched]
                    i = ntasks_dispatched
                    log.log(VERBOSE, "Sent task {0} to worker {1} with tag {2}."
                            .format(type(task), worker, i))

                    # Send out the tasks asynchronously.
                    self.comm.isend(task, dest=worker, tag=i)
                    ntasks_dispatched += 1

            return results

    def bcast(self, *args, **kwargs):
        """
        Equivalent to mpi4py :func:`bcast` collective operation.
        """
        return self.comm.bcast(*args, **kwargs)

    def close(self):
        """
        Just send a message off to all the pool members which contains
        the special :class:`_close_pool_message` sentinel.

        """
        if self.is_master():
            for i in range(self.size):
                self.comm.isend(_close_pool_message(), dest=i + 1)

class MPIPool2(GenericPool):
    """
    This implementation is based on the code here:
    https://github.com/juliohm/HUM/blob/master/pyhum/utils.py#L24
    """

    def __init__(self, comm=None, wait_on_start=True):

        if MPI is None:
            raise ImportError("Please install mpi4py")

        if comm is None:
            comm = MPI.COMM_WORLD
        self.comm = comm

        self.master = 0
        self.rank = self.comm.Get_rank()
        self.workers = set(range(self.comm.size))
        self.workers.discard(self.master)

        self.size = self.comm.Get_size() - 1

        if self.size == 0:
            raise ValueError("Tried to create an MPI pool, but there "
                             "was only one MPI process available. "
                             "Need at least two.")

        # Enter main loop for workers?
        if wait_on_start:
            if self.is_worker():
                self.wait()

    @staticmethod
    def enabled():
        if MPI is not None:
            if MPI.COMM_WORLD.size > 1:
                return True
        return False

    def wait(self):
        """
        Make the workers listen to the master.
        """
        if self.is_master():
            return

        worker = self.comm.rank
        status = MPI.Status()
        while True:
            log.log(VERBOSE, "Worker {0} waiting for task".format(worker))

            task = self.comm.recv(source=self.master, tag=MPI.ANY_TAG, status=status)

            if task is None:
                log.log(VERBOSE, "Worker {0} told to quit work".format(worker))
                break

            func, arg = task
            log.log(VERBOSE, "Worker {0} got task {1} with tag {2}"
                    .format(worker, arg, status.tag))

            result = func(arg)

            log.log(VERBOSE, "Worker {0} sending answer {1} with tag {2}"
                    .format(worker, result, status.tag))

            self.comm.ssend(result, self.master, status.tag)

    def map(self, func, iterable):
        """
        Evaluate a function at various points in parallel. Results are
        returned in the requested order (i.e. y[i] = f(x[i])).
        """

        # If not the master just wait for instructions.
        if not self.is_master():
            self.wait()
            return

        workerset = self.workers.copy()
        tasklist = [(tid, (func, arg)) for tid, arg in enumerate(iterable)]
        resultlist = [None] * len(tasklist)
        pending = len(tasklist)

        while pending:
            if workerset and tasklist:
                worker = workerset.pop()
                taskid, task = tasklist.pop()
                log.log(VERBOSE, "Sent task {0} to worker {1} with tag {2}"
                        .format(task[1], worker, taskid))
                self.comm.send(task, dest=worker, tag=taskid)

            if tasklist:
                flag = self.comm.Iprobe(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG)
                if not flag:
                    continue
            else:
                self.comm.Probe(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG)

            status = MPI.Status()
            result = self.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
            worker = status.source
            taskid = status.tag
            log.log(VERBOSE, "Master received from worker {0} with tag {1}"
                    .format(worker, taskid))

            workerset.add(worker)
            resultlist[taskid] = result
            pending -= 1

        return resultlist

    def close(self):
        """
        Tell all the workers to quit work.
        """
        if self.is_worker():
            return

        for worker in self.workers:
            self.comm.send(None, worker, 0)

class MPIOptimizedPool(GenericPool):
    '''
    An optimized but highly specific version of the MPI pool. Iterables passed to the
    ``map`` method must be arrays of numpy floats. The return value of the mapping
    function must be a scalar float.

    If the length of the array passed to the ``map`` method is larger than the number
    of processes, the iterable is passed in chunks, which are processed *serially*
    on each processor. This minimizes back-and-forth communication and therefore
    increases the speed. This implementation also solves an MPI hanging issue I was
    having on Hyak.

    '''

    def __init__(self, comm = None, debug = False,
                 wait_on_start = True, exit_on_end = True, **kwargs):
        '''

        '''

        if MPI is None:
            raise ImportError("Please install mpi4py.")
        if comm is None:
            comm = MPI.COMM_WORLD
        if comm.size <= 1:
            raise ValueError("Tried to create an MPI pool, but there "
                             "was only one MPI process available. "
                             "Need at least two.")
        self.comm = comm
        self.workers = set(range(comm.size))
        self.workers.discard(0)
        self.function = _placeholder_function
        self.dims = (1,1)
        self.size = self.comm.Get_size() - 1
        self.rank = self.comm.Get_rank()
        self.debug = debug
        self.exit_on_end = exit_on_end

        # Enter main loop for workers?
        if wait_on_start:
            if self.is_worker():
                self.wait()

    @staticmethod
    def enabled():
        '''

        '''

        if MPI is not None:
            if MPI.COMM_WORLD.size > 1:
                return True
        return False

    def map(self, function, iterable):
        '''

        '''

        assert self.is_master(), "Must be master to call ``map``."

        # Setup
        assert type(iterable) is list or type(iterable) is np.ndarray, \
               "``iterable`` must either be a list or np.ndarray instance."
        if isinstance(iterable, list):
            iterable = np.array(iterable, dtype = 'float64')
        if iterable.ndim == 1:
            iterable = np.atleast_2d(iterable).T
        assert iterable.ndim == 2, "``iterable`` arg to ``map`` must be 1- or 2-d."

        results = [None for i in range(self.size)]

        # Determine the size of the arrays to be sent to the workers
        q, r = divmod(iterable.shape[0], self.size)
        if r == 0:
            dims = (q, iterable.shape[1])
        else:
            dims = (q + 1, iterable.shape[1])

        # Construct our ``tasks`` matrix. If the number of tasks does not
        # divide evenly into the number of workers, we set some tasks to ``np.nan``s
        tasks = np.empty((self.size, dims[0], dims[1]), dtype = 'float64')
        nans = np.empty(dims[1]); nans.fill(np.nan)
        FULL = np.zeros(self.size, dtype = int); FULL[:r] = 1
        a = 0
        for i in range(self.size):
            b = a + q + FULL[i]
            if r and not FULL[i]:
                tasks[i] = np.vstack([iterable[a:b], nans])
            else:
                tasks[i] = iterable[a:b]
            a = b

        # New function?
        if (function != self.function):
            # Tell the workers an update is on its way
            for worker in self.workers:
                self.comm.Send(np.empty(self.dims, dtype = 'float64'),
                               dest = worker, tag = TAG_NEW_FUNC)

            # Get a thumbs-up from the workers. MPI hangs if we skip this step...
            for i in range(self.size):
                status = MPI.Status()
                result = MSG_EMPTY()
                self.comm.Recv(result, source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                               status = status)
                assert status.Get_tag() == TAG_NEW_FUNC, ("Tag mismatch. Expected "
                                                          "tag %d, but worker %d "
                                                          "sent tag %d." %
                                                          (TAG_NEW_FUNC,
                                                          status.Get_source(),
                                                          status.Get_tag()))
            # Send the function over to each of the workers
            for w, worker in enumerate(self.workers):
                self.comm.send(function, dest = worker, tag = TAG_NEW_FUNC)
                status = MPI.Status()
                result = MSG_EMPTY()
                self.comm.Recv(result, source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                               status = status)
                assert status.Get_tag() == TAG_NEW_FUNC, ("Tag mismatch. Expected "
                                                          "tag %d, but worker %d "
                                                          "sent tag %d." %
                                                          (TAG_NEW_FUNC,
                                                          status.Get_source(),
                                                          status.Get_tag()))

            # Update the function locally
            self.function = function

        # New dimensions?
        if (dims != self.dims):
            # Tell the workers an update is on its way
            for worker in self.workers:
                self.comm.Send(np.empty(self.dims, dtype = 'float64'),
                               dest = worker, tag = TAG_NEW_DIMS)

            # Get a thumbs-up from the workers. MPI hangs if we skip this step...
            for i in range(self.size):
                status = MPI.Status()
                result = MSG_EMPTY()
                self.comm.Recv(result, source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                               status = status)
                assert status.Get_tag() == TAG_NEW_DIMS, ("Tag mismatch. Expected "
                                                          "tag %d, but worker %d "
                                                          "sent tag %d." %
                                                          (TAG_NEW_DIMS,
                                                          status.Get_source(),
                                                          status.Get_tag()))

            # Send the dims over to each of the workers
            for w, worker in enumerate(self.workers):
                self.comm.send(dims, dest = worker, tag = TAG_NEW_DIMS)
                status = MPI.Status()
                result = MSG_EMPTY()
                self.comm.Recv(result, source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                               status = status)
                assert status.Get_tag() == TAG_NEW_DIMS, ("Tag mismatch. Expected "
                                                          "tag %d, but worker %d "
                                                          "sent tag %d." %
                                                          (TAG_NEW_DIMS,
                                                          status.Get_source(),
                                                          status.Get_tag()))

            # Update locally
            self.dims = dims

        # Now we send the actual tasks off
        for w, worker in enumerate(self.workers):
            self.comm.Send(tasks[w], dest = worker, tag = TAG_TASK)

        # Receive the tasks (not in order!)
        for i in range(self.size):
            status = MPI.Status()
            result = np.empty(self.dims[0], dtype = 'float64')
            result = self.comm.recv(source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                           status = status)
            assert status.Get_tag() == TAG_TASK, ("Tag mismatch. Expected "
                                                  "tag %d, but worker %d "
                                                  "sent tag %d." %
                                                  (TAG_TASK,
                                                  status.Get_source(),
                                                  status.Get_tag()))

            # Get the worker number
            w = status.Get_source() - 1

            # Populate the results list, removing any NaNs
            if FULL[w]:
                results[w] = result
            else:
                results[w] = result[:-1]

        # Flatten and return
        results = [item for sublist in results for item in sublist]

        return results

    def wait(self):
        '''

        '''

        if not self.is_worker():
            return

        # Receive and work on tasks
        status = MPI.Status()
        while True:
            # Receive the task array
            task = np.empty(self.dims, dtype = 'float64')
            self.comm.Recv(task, source = 0, tag = MPI.ANY_TAG, status = status)
            tag = status.Get_tag()

            if tag == TAG_TASK:
                # Business as usual: the parameter array
                result = self.apply_function(task)
                self.comm.send(result, dest = 0, tag = tag)
            else:
                # Confirm receipt
                self.comm.Send(MSG_EMPTY(), dest = 0, tag = tag)

                # Receive the info
                status = MPI.Status()
                obj = self.comm.recv(source = 0, tag = MPI.ANY_TAG, status=status)
                assert tag == status.Get_tag(), ("Tag mismatch. Worker %d expected "
                                                 "tag %d, but master sent tag %d." %
                                                 (self.rank, tag, status.Get_tag()))

                # Close the pool?
                if tag == TAG_CLOSE:
                    break

                # Update the mapping function?
                elif tag == TAG_NEW_FUNC:
                    self.function = obj

                # Update the input array size?
                elif tag == TAG_NEW_DIMS:
                    self.dims = obj

                # Communicate back
                self.comm.Send(MSG_EMPTY(), dest = 0, tag = tag)

        # Kill the process?
        if self.exit_on_end:
            sys.exit()

    def apply_function(self, task):
        '''

        '''
        return [self.function(x) for x in task]

        result = np.zeros(task.shape[0])
        for i, x in enumerate(task):
            if np.all(np.isnan(x)):
                result[i] = np.nan
            else:
                result[i] = self.function(x)
        return result

    def close(self):
        '''

        '''

        if not self.is_master():
          return

        # Tell the workers an update is on its way
        for worker in self.workers:
            self.comm.Send(np.empty(self.dims, dtype = 'float64'),
                           dest = worker, tag = TAG_CLOSE)

        # Get a thumbs-up from the workers. MPI hangs if we skip this step...
        for i in range(self.size):
            status = MPI.Status()
            result = MSG_EMPTY()
            self.comm.Recv(result, source = MPI.ANY_SOURCE, tag = MPI.ANY_TAG,
                           status = status)
            assert status.Get_tag() == TAG_CLOSE, ("Tag mismatch. Expected "
                                                   "tag %d, but worker %d "
                                                   "sent tag %d." %
                                                   (TAG_CLOSE, status.Get_source(),
                                                   status.Get_tag()))

        # Send the function over to each of the workers
        for w, worker in enumerate(self.workers):
            self.comm.send(None, dest = worker, tag = TAG_CLOSE)

class SerialPool(GenericPool):

    def __init__(self, **kwargs):
        self.size = 0
        self.rank = 0

    @staticmethod
    def enabled():
        return True

    def wait(self):
        raise Exception('``SerialPool`` told to wait!')

    def map(self, function, iterable):
        return list(map(function, iterable))

# ----------------------------------------------------------------------------

def _initializer_wrapper(actual_initializer, *rest):
    """
    We ignore SIGINT. It's up to our parent to kill us in the typical
    condition of this arising from ``^C`` on a terminal. If someone is
    manually killing us with that signal, well... nothing will happen.

    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if actual_initializer is not None:
        actual_initializer(*rest)

class MultiPool(multiprocessing.pool.Pool):
    """
    This is simply ``emcee``'s :class:`InterruptiblePool`.

    A modified version of :class:`multiprocessing.pool.Pool` that has better
    behavior with regard to ``KeyboardInterrupts`` in the :func:`map` method.

    Contributed by Peter K. G. Williams <peter@newton.cx>.

    :param processes: (optional)
        The number of worker processes to use; defaults to the number of CPUs.

    :param initializer: (optional)
        Either ``None``, or a callable that will be invoked by each worker
        process when it starts.

    :param initargs: (optional)
        Arguments for *initializer*; it will be called as
        ``initializer(*initargs)``.

    :param kwargs: (optional)
        Extra arguments. Python 2.7 supports a ``maxtasksperchild`` parameter.

    """
    wait_timeout = 3600

    def __init__(self, processes=None, initializer=None, initargs=(),
                 **kwargs):
        new_initializer = functools.partial(_initializer_wrapper, initializer)
        super(MultiPool, self).__init__(processes, new_initializer,
                                        initargs, **kwargs)
        self.size = 0

    @staticmethod
    def enabled():
        '''

        '''

        return True

    def map(self, func, iterable, chunksize=None):
        """
        Equivalent of ``map()`` built-in, without swallowing
        ``KeyboardInterrupt``.

        :param func:
            The function to apply to the items.

        :param iterable:
            An iterable of items that will have `func` applied to them.

        """
        # The key magic is that we must call r.get() with a timeout, because
        # a Condition.wait() without a timeout swallows KeyboardInterrupts.
        r = self.map_async(func, iterable, chunksize)

        while True:
            try:
                return r.get(self.wait_timeout)
            except multiprocessing.TimeoutError:
                pass
            except KeyboardInterrupt:
                self.terminate()
                self.join()
                raise

def choose_pool(mpi=False, processes=1, **kwargs):
    """
    Chooses between the different pools.
    """

    if mpi:
        if not MPIPool2.enabled():
            raise SystemError("Tried to run with MPI but MPIPool not enabled.")

        pool = MPIPool2(**kwargs)
        if not pool.is_master():
            sys.exit(0)

        log.info("Running with MPI")
        return pool

    elif processes != 1 and MultiPool.enabled():
        log.info("Running with multiprocessing on {} cores".format(processes))
        return MultiPool(processes=processes, **kwargs)

    else:
        log.info("Running serial")
        return SerialPool(**kwargs)

# ----------------------------------------------------------------------------

def _test_function(x):
    '''
    Wastes a random amount of time, then
    returns the average of ``x``.

    '''

    for i in range(np.random.randint(99999)):
        j = i ** 2

    return np.sum(x) / float(len(x))

# if __name__ == '__main__':

    # # Instantiate the pool
    # with choose_pool() as pool:

    #     # The iterable we'll apply ``_test_function`` to
    #     walkers = np.array([[i, i] for i in range(100)], dtype = 'float64')

    #     # Use the pool to map ``walkers`` onto the function
    #     res = pool.map(_test_function, walkers)

    #     # Check if the parallelization worked
    #     assert np.allclose(res, [TestFunction(w) for w in walkers])

    #     print("%s: success!" % type(pool).__name__)
