# -*- coding: utf-8 -*-

import asyncio
from collections import namedtuple, OrderedDict
import datetime
import importlib
import logging
import signal
import sys
import threading
import traceback

from . import eventloop
from .metadata import Metadata


logger = logging.getLogger(__name__)


class ModuleManager(object):
    """Class to handle modules"""
    _modules = OrderedDict()  # Dictionary of module data

    def __init__(self, appmodules, exception_path=None):
        """Initialization"""
        self._exception_path = exception_path
        self._modules = OrderedDict()
        self._thread_reference = threading.current_thread() 
        self._running_tasks = set()
        self._finished_tasks = set()
        self._exit = False
        self.register_modules(appmodules)

    def create_metadata(self):
        """Create a new metadata object"""
        return Metadata(source_obj=self, source_name='modulemanager')

    def register_module(self, modulename, moduleunit):
        """Register a module"""
        module = moduleunit.module_class
        module_obj = module(modulename, self.function_references)
        self._modules[modulename] = module_obj

    def register_modules(self, appmodules):
        if appmodules is not None:
            for modulename, moduleunit in appmodules.items():
                self.register_module(modulename, moduleunit)

    def is_ready_module(self, modulename):
        """Checks whether a module is available and ready"""
        if not (modulename in self._modules):
            return False
        return self._modules[modulename].is_ready

    def task_done_callback(self, task):
        self._running_tasks.discard(task)
        self._finished_tasks.add(task)
        try:
            task.result()
        except Exception as e:
            # Print exceptions and log to file
            logger.critical(f'Exception occured: [{str(e)}]')
            logger.exception('Exception info:')  # just error but prints traceback
            if self._exception_path is not None:
                with open(self._exception_path, 'a') as handle:
                    handle.write(datetime.datetime.now().isoformat(sep=' '))
                    handle.write('\n')
                    traceback.print_exc(file=handle)
                    handle.write('\n')                

    async def call_method_async(self, module, methodname, log_unknown=True, **kwargs):

        async def wait_for_free_task_slot():
            if len(self._running_tasks) > 2 * len(self._modules):
                logger.info('Waiting for free slot before starting the next task')
                sleeptime = 0.001  # start with one millisecond
                while len(self._running_tasks) > len(self._modules):
                    await asyncio.sleep(sleeptime)
                    if sleeptime < 1:
                        sleeptime *= 2  # double sleeptime in each iteration
                    else:
                        logger.warning('Starting the next task after a long wait; check reasons for long running tasks')
                        break  # don't wait indefinitely

        logger.debug(f'Calling method asynchronously [{module}.{methodname}({str(kwargs)})]')
        await wait_for_free_task_slot()
        task = asyncio.create_task(module.call_method(methodname, log_unknown, **kwargs))
        self._running_tasks.add(task)
        task.add_done_callback(self.task_done_callback)
        return task

    async def exec_task_internal(self, target, metadata, asynchronous=False, **kwargs):
        """Execute the specified task (target specifies the method to be called) synchronously with the given arguments"""
        logger.debug(f'Executing task [{target}({str(kwargs)})]')
        modulename, _, methodname = target.partition('.')
        if not self.is_ready_module(modulename):
            logger.error(f'Method module [{target}] is in an inactive state or unknown module was tried to be called')
            return None
        module = self._modules.get(modulename)
        if module is None:
            logger.error(f'Unknown module [{modulename}] for task [{target}]')
        else:
            if asynchronous:
                await self.call_method_async(module, methodname, **kwargs)
                return False            
            else:
                return await module.call_method(methodname, **kwargs)

    def exec_task_threadsafe(self, target, metadata, asynchronous=False, **kwargs):
        """Execute a task while ensuring that no other task is running in parallel"""
        logger.debug(f'Executing task [{target}({str(kwargs)})] in a threadsafe manner')
        task = asyncio.run_coroutine_threadsafe(self.exec_task_internal(target, metadata, asynchronous, **kwargs), asyncio.get_running_loop())
        result = task.result()  # this will block until the result is available
        return result

    async def exec_task(self, target, metadata, asynchronous=False, **kwargs):
        """Execute the specified task (target specifies the method to be called) synchronously with the given arguments, getting a lock if needed"""
        if self._thread_reference == threading.current_thread():
            return await self.exec_task_internal(target, metadata, asynchronous, **kwargs)
        else:
            return self.exec_task_threadsafe(target, metadata, asynchronous, **kwargs)

    async def broadcast_event_internal(self, event, metadata, asynchronous=True, **kwargs):
        """Immediately send the specified event with the given arguments to all participants with a matching event handler"""
        logger.debug(f'Broadcasting event [{event}({str(kwargs)})')
        for modulename, module_obj in self._modules.items():
            if metadata.source_obj != module_obj:  # split horizon, don't provide event to source
                if asynchronous:
                    await self.call_method_async(module_obj, event, log_unknown=False, **kwargs)
                else:
                    await module_obj.call_method(event, log_unknown=False, **kwargs)
        if event == 'on_exit':
            self._exit = True
            await self.broadcast_event_internal('deactivate', metadata=self.create_metadata(), asynchronous=False)
            await self.broadcast_event_internal('initiate_shutdown', metadata=self.create_metadata(), asynchronous=False)
            await self.broadcast_event_internal('finalize_shutdown', metadata=self.create_metadata(), asynchronous=False)

    def broadcast_event_threadsafe(self, event, metadata, asynchronous=True, **kwargs):
        """Handle an event while ensuring that no other task is running in parallel"""
        logger.debug(f'Broadcasting event [{event}({str(kwargs)})] in a threadsafe manner')
        asyncio.run_coroutine_threadsafe(self.broadcast_event_internal(event=event, metadata=metadata, asynchronous=asynchronous, **kwargs), asyncio.get_running_loop())

    async def broadcast_event(self, event, metadata, asynchronous=True, **kwargs):
        """Handle an event, getting a lock if needed"""
        if self._thread_reference == threading.current_thread():
            return await self.broadcast_event_internal(event=event, metadata=metadata, asynchronous=asynchronous, **kwargs)
        else:
            return self.broadcast_event_threadsafe(event=event, metadata=metadata, asynchronous=asynchronous, **kwargs)

    async def enqueue_task_internal(self, target, metadata, **kwargs):
        """Enqueue the provided task for asynchronous execution"""
        logger.debug(f'Enqueuing task [{target}({str(kwargs)})]')
        await self._eventloop.queue.put(target=target, metadata=metadata, **kwargs)

    def enqueue_task_threadsafe(self, target, metadata, **kwargs):
        """Enqueue the provided task for asynchronous execution"""
        logger.debug(f'Enqueuing task [{target}({str(kwargs)})] in a threadsafe manner')
        asyncio.run_coroutine_threadsafe(self.enqueue_task_internal(target=target, metadata=metadata, **kwargs), asyncio.get_running_loop())

    async def enqueue_task(self, target, metadata, **kwargs):
        """Enqueue the provided task for asynchronous execution"""
        if self._thread_reference == threading.current_thread():
            return await self.enqueue_task_internal(target=target, metadata=metadata, **kwargs)
        else:
            return self.enqueue_task_threadsafe(target=target, metadata=metadata, **kwargs)

    async def trigger_event_internal(self, target, metadata, **kwargs):
        """Enqueue the provided event for asynchronous event handling"""
        logger.debug(f'Triggering event target [{target}({str(kwargs)})]')        
        await self._eventloop.queue.put(target=target, metadata=metadata, kwargs=kwargs)

    def trigger_event_threadsafe(self, target, metadata, **kwargs):
        """Enqueue the provided event for asynchronous event handling"""
        logger.debug(f'Triggering event target [{target}({str(kwargs)})] in a threadsafe manner')
        asyncio.run_coroutine_threadsafe(self.trigger_event_internal(target=target, metadata=metadata, args=args), asyncio.get_running_loop())

    async def trigger_event(self, event, metadata, **kwargs):
        """Enqueue the provided event for asynchronous event handling"""
        target = 'on_' + event
        if self._thread_reference == threading.current_thread():
            return await self.trigger_event_internal(target=target, metadata=metadata, **kwargs)
        else:
            return self.trigger_event_threadsafe(target=target, metadata=metadata, **kwargs)

    async def process_item(self, item):
        """Process an item from the event queue"""
        if '.' in item.target:
            await self.exec_task(method=item.target, metadata=item.metadata, asynchronous=True, **(item.kwargsargs))            
        else:
            await self.broadcast_event(event=item.target, metadata=item.metadata, **(item.kwargs))

    async def gather_finished_tasks(self):
        """Gather all finished tasks"""
        await asyncio.gather(*self._finished_tasks, return_exceptions=True)
        self._finished_tasks.clear()

    async def queue_empty(self):
        """React on empty event queue"""
        await self.gather_finished_tasks()  # clean up finished stuff
        await self.broadcast_event('becoming_idle', metadata=self.create_metadata())
        if self._exit:
            await asyncio.gather(*self._running_tasks, return_exceptions=True)
            if not len(self._running_tasks):
                return True
        return False

    async def maintask(self):
        """Main task handling the lifecycle"""
        # Create and start event loop
        self._eventloop = eventloop.EventLoop(self.process_item, self.queue_empty)
        task_eventloop = asyncio.create_task(self._eventloop.run_eventloop())
        # Initialize modules
        await self.broadcast_event_internal('startup', metadata=self.create_metadata(), asynchronous=False)
        await self.broadcast_event_internal('activate', metadata=self.create_metadata(), asynchronous=False)
        # Wait for the event loop to terminate
        await task_eventloop
        # Final clean-up
        await self.gather_finished_tasks()

    def on_signal(self, signum, handler):
        """React on a received operating system signal"""
        raise(KeyboardInterrupt)  # react on signal like as with a keyboard interrupt

    def run(self):
        """Run the program"""
        # Register signal handler
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        # Run asyncio loop
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            maintask = loop.create_task(self.maintask())
            done = False
            while not done:
                try:
                    loop.run_until_complete(maintask)
                    done = True
                except KeyboardInterrupt:
                    logger.info('Keyboard interrupt received. Exiting...')
                    # Shutdown by broadcasting shutdown event
                    task = loop.create_task(self.trigger_event(event='exit', metadata=self.create_metadata()))
                    self._running_tasks.add(task)
                    task.add_done_callback(self.task_done_callback)
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                logger.warning(f'Cancelling task [{task}]')
                task.cancel()
            loop.stop()
        finally:
            loop.close()

    @property
    def function_references(self):
        """Return reference to functions for calling from external modules"""
        FunctionReferences = namedtuple('FunctionReferences', ['trigger_event', 'enqueue_task', 'exec_task', 'broadcast_event', 'call_method_async'])
        return FunctionReferences(self.trigger_event, self.enqueue_task, self.exec_task, self.broadcast_event, self.call_method_async)