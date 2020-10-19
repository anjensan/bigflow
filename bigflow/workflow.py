import abc
import datetime as dt
import typing

from collections import OrderedDict
from typing import Optional

import bigflow
from bigflow.commons import now


import logging
logger = logging.getLogger(__name__)


DEFAULT_SCHEDULE_INTERVAL = '@daily'


def get_timezone_offset_seconds() -> int:
    return dt.datetime.now().astimezone().tzinfo.utcoffset(None).seconds


def hourly_start_time(start_time: dt.datetime) -> dt.datetime:
    td = dt.timedelta(seconds=get_timezone_offset_seconds())
    return start_time.replace(microsecond=0) - td


def daily_start_time(start_time: dt.datetime) -> dt.datetime:
    td = dt.timedelta(hours=24)
    return start_time.replace(hour=0, minute=0, second=0, microsecond=0) - td


class JobContext(typing.NamedTuple):
    runtime: typing.Optional[dt.datetime]
    runtime_as_str: typing.Optional[str]
    workflow: typing.Optional['Workflow']
    # TODO: add unique 'workflow execution id' (for tracing/logging)


class Job(abc.ABC):
    """Base abstract class for bigflow.Jobs.  It is recommended to inherit all your jobs from this class."""

    retries: int = 3
    retry_delay: float = 60

    @property
    @abc.abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def execute(self, context: JobContext):
        raise NotImplementedError


class Workflow(object):

    RUNTIME_FORMATS = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]

    def __init__(
        self,
        workflow_id,
        definition,
        schedule_interval=DEFAULT_SCHEDULE_INTERVAL,
        start_time_factory: typing.Callable[[dt.datetime], dt.datetime] = daily_start_time,
        log_config: typing.Optional['bigflow.log.LogConfigDict'] = None,
    ):
        self.definition = self._parse_definition(definition)
        self.schedule_interval = schedule_interval
        self.workflow_id = workflow_id
        self.start_time_factory = start_time_factory
        self.log_config = log_config

    def _parse_runtime_str(self, runtime: str):
        for format in self.RUNTIME_FORMATS:
            try:
                return dt.datetime.strptime(runtime, format)
            except ValueError:
                pass
        raise ValueError("Unable to parse 'run_time' %r" % rt)

    def _execute_job(self, job, context):
        if not isinstance(job, Job):
            logger.debug("Please, inherit your job %r from `bigflow.Job` class", job)
        if hasattr(job, 'execute'):
            job.execute(context)
        else:
            logger.warn("Old bigflow.Job api is used: please change `run(runtime) => execute(context)`)")
            raise Exception("XXX")
            # fallback to old api
            job.run(context.runtime_as_str)

    def _make_job_context(self, runtime_raw):
        if isinstance(runtime_raw, str):
            runtime_as_str = runtime_raw
            runtime = self._parse_runtime_str(runtime_raw)
        else:
            runtime = runtime_raw or dt.datetime.now()
            runtime_as_str = runtime.strftime(self.RUNTIME_FORMATS[0])

        return JobContext(
            workflow=self,
            runtime=runtime,
            runtime_as_str=runtime_as_str,
        )        

    def run(self, runtime: typing.Union[dt.datetime, str, None] = None):
        context = self._make_job_context(runtime)
        for job in self.build_sequential_order():
            self._execute_job(job, context)

    def find_job(self, job_id):
        for job_wrapper in self.build_sequential_order():
            if job_wrapper.job.id == job_id:
                return job_wrapper.job
        raise ValueError(f'Job {job_id} not found.')

    def run_job(self, job_id: str, runtime: typing.Union[dt.datetime, str, None] = None):
        context = self._make_job_context(runtime)
        self._execute_job(self.find_job(job_id), context)

    def build_sequential_order(self):
        return self.definition.sequential_order()

    def call_on_graph_nodes(self, consumer):
        self.definition.call_on_graph_nodes(consumer)

    def _parse_definition(self, definition):
        if isinstance(definition, list):
            return Definition(self._map_to_workflow_jobs(definition))
        if isinstance(definition, Definition):
            return definition
        raise ValueError("Invalid argument %s" % definition)

    @staticmethod
    def _map_to_workflow_jobs(job_list):
        return [WorkflowJob(job, i) for i, job in enumerate(job_list)]


class WorkflowJob:
    def __init__(self, job, name):
        self.job = job
        self.name = name

    def run(self, runtime):
        self.job.run(runtime=runtime)

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return self.name == other.name

    def __repr__(self):
        return "WorkflowJob{job=..., name=%s}" % self.name


class Definition:
    def __init__(self, jobs: dict):
        self.job_graph = self._build_graph(jobs)
        self.job_order_resolver = JobOrderResolver(self.job_graph)

    def sequential_order(self):
        return self.job_order_resolver.find_sequential_run_order()

    def call_on_graph_nodes(self, consumer):
        return self.job_order_resolver.call_on_graph_nodes(consumer)

    def _build_graph(self, jobs):
        if isinstance(jobs, list):
            job_graph = self._convert_list_to_graph(jobs)
        elif isinstance(jobs, dict):
            job_graph = {self._map_to_workflow_job(source_job): [self._map_to_workflow_job(tj) for tj in target_jobs]
                         for source_job, target_jobs in jobs.items()}
        else:
            raise ValueError("Job graph has to be dict or list")

        JobGraphValidator(job_graph).validate()
        return job_graph

    def _map_to_workflow_job(self, job):
        if not isinstance(job, WorkflowJob):
            job = WorkflowJob(job, job.id)
        return job

    @staticmethod
    def _convert_list_to_graph(job_list):
        graph_as_dict = OrderedDict()
        if len(job_list) == 1:
            graph_as_dict[job_list[0]] = []
        else:
            for i in range(1, len(job_list)):
                graph_as_dict[job_list[i - 1]] = [job_list[i]]
        return graph_as_dict


class InvalidJobGraph(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __repr__(self):
        return self.msg


class JobGraphValidator:
    def __init__(self, job_graph):
        self.job_graph = job_graph

    def validate(self):
        self._validate_if_not_cyclic()

    def _validate_if_not_cyclic(self):
        visited = set()
        stack = set()
        for job in self.job_graph:
            self._validate_job(job, visited, stack)

    def _validate_job(self, job, visited, stack):
        if job in stack:
            raise InvalidJobGraph(f"Found cyclic dependency on job {job}")
        if job in visited:
            return

        visited.add(job)
        if not job in self.job_graph:
            return
        stack.add(job)
        for dep in self.job_graph[job]:
            self._validate_job(dep, visited, stack)
        stack.remove(job)


class JobOrderResolver:
    def __init__(self, job_graph):
        self.job_graph = job_graph
        self.parental_map = self._build_parental_map()

    def find_sequential_run_order(self):
        ordered_jobs = []

        def add_to_ordered_job(job, dependencies):
            ordered_jobs.append(job)

        self.call_on_graph_nodes(add_to_ordered_job)
        return ordered_jobs

    def call_on_graph_nodes(self, consumer):
        visited = set()
        for job in self.parental_map:
            self._call_on_graph_node_helper(
                job, self.parental_map, visited, consumer)

    def _build_parental_map(self):
        visited = set()
        parental_map = OrderedDict()
        for job in self.job_graph:
            self._fill_parental_map(job, parental_map, visited)
        return parental_map

    def _fill_parental_map(self, job, parental_map, visited):
        if job not in self.job_graph or job in visited:
            return
        visited.add(job)

        if job not in parental_map:
            parental_map[job] = []

        for dependency in self.job_graph[job]:
            if dependency not in parental_map:
                parental_map[dependency] = []
            parental_map[dependency].append(job)
            self._fill_parental_map(dependency, parental_map, visited)

    def _call_on_graph_node_helper(self, job, parental_map, visited, consumer):
        if job in visited:
            return
        visited.add(job)

        for parent in parental_map[job]:
            self._call_on_graph_node_helper(
                parent, parental_map, visited, consumer)
        consumer(job, parental_map[job])
