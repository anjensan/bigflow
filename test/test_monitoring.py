import logging
from unittest.mock import patch

from unittest import TestCase, mock
from datetime import datetime

from google.cloud import logging_v2

from bigflow import monitoring
from freezegun import freeze_time

from bigflow.logger import GCPLogger, log_job_run_failures

TEST_DATETIME = datetime(year=2019, month=1, day=2)
TEST_DATETIME_RFC3339 = '2019-01-02T00:00:00Z'
logger = logging.getLogger(__name__)

class FormatRFC3339TestCase(TestCase):

    def test_should_format_datetime(self):
        # expect
        self.assertEqual(
            monitoring.format_rfc3339(TEST_DATETIME),
            TEST_DATETIME_RFC3339)


class GetStartTimeTestCase(TestCase):

    @freeze_time(TEST_DATETIME)
    def test_should_return_utc_now_minus_5_minutes(self):
        # expect
        self.assertEqual(
            monitoring.get_start_time(),
            '2019-01-01T23:55:00Z')


class GetNowRFC3339(TestCase):

    @freeze_time(TEST_DATETIME)
    def test_should_return_now_as_rfc3339(self):
        # expect
        self.assertEqual(
            monitoring.get_now_rfc3339(),
            TEST_DATETIME_RFC3339)


class MetricExistsTestCase(TestCase):

    @mock.patch('bigflow.monitoring.api_list_metrics')
    def test_should_check_if_metric_exists(self, api_list_metrics_mock: mock.Mock):
        # given
        api_list_metrics_mock.return_value = {'metricDescriptors': 'something'}
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        # expect
        self.assertTrue(monitoring.metric_exists('client', monitoring_config, 'metric_type', logger))
        api_list_metrics_mock.assert_has_calls([mock.call('client', 'projects/test project', 'metric_type')])

        # given
        api_list_metrics_mock.return_value = {}

        # expect
        self.assertFalse(monitoring.metric_exists('client', monitoring_config, 'metric_type', logger))


class WaitForMetricTestCase(TestCase):

    @mock.patch('bigflow.monitoring.metric_exists')
    def test_should_wait_until_metric_exists(self, metric_exists_mock):
        # given
        metric_exists_mock.return_value = True
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        # expect
        self.assertTrue(monitoring.wait_for_metric('client', monitoring_config, 'metric_type', logger))

    @mock.patch('bigflow.monitoring.metric_exists')
    def test_should_raise_exception_when_no_retries_left(self, metric_exists_mock):
        # given
        metric_exists_mock.return_value = False
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        # expect
        with self.assertRaises(monitoring.MetricError):
            monitoring.wait_for_metric('client', monitoring_config, 'metric_type', logger)


class CreateTimeseriesData(TestCase):
    def test_should_return_timeseries_json(self):
        # expect
        self.assertEqual(
            monitoring.create_timeseries_data('metric type', 'job id', 'project id', 'region', 'env name', 'start',
                                              'end'), {
                "metric": {
                    "type": 'metric type',
                    "labels": {
                        "job_id": 'job id'
                    }
                },
                "resource": {
                    "type": 'cloud_composer_environment',
                    "labels": {
                        'project_id': 'project id',
                        'location': 'region',
                        'environment_name': 'env name'
                    }
                },
                "points": [
                    {
                        "interval": {
                            "startTime": 'start',
                            "endTime": 'end'
                        },
                        "value": {
                            "int64Value": 1
                        }
                    }
                ]
            })


class IncrementCounterTestCase(TestCase):

    @freeze_time(TEST_DATETIME)
    @mock.patch('bigflow.monitoring.api_create_timeseries')
    def test_should_call_api_create_timeseries(self, api_create_timeseries_mock: mock.Mock):
        # given
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        # when
        monitoring.increment_counter('client', monitoring_config, 'metric type', 'job id')

        # then
        api_create_timeseries_mock.assert_called_once_with(
            'client',
            monitoring_config,
            monitoring.create_timeseries_data(
                'metric type',
                'job id',
                'test project',
                'eu-west',
                'env',
                TEST_DATETIME_RFC3339,
                TEST_DATETIME_RFC3339
            ))


class IncrementJobFailureCount(TestCase):

    @mock.patch('bigflow.monitoring.api_client')
    @mock.patch('bigflow.monitoring.increment_counter')
    @mock.patch('bigflow.monitoring.wait_for_metric')
    @mock.patch('bigflow.monitoring.api_create_metric')
    @mock.patch('bigflow.monitoring.metric_exists')
    def test_should_create_metric_if_not_found(self,
                                               metric_exists_mock,
                                               api_create_metric_mock,
                                               wait_for_metric_mock,
                                               increment_counter_mock,
                                               api_client_mock):
        # given
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')
        api_client_mock.return_value = 'client'
        metric_exists_mock.return_value = False

        # when
        monitoring.increment_job_failure_count(monitoring_config, 'job id', logger)

        # then
        api_create_metric_mock.assert_called_once_with(
            'client',
            monitoring_config.project_resource,
            monitoring.BIGFLOW_JOB_FAILURE_METRIC)

    @mock.patch('bigflow.monitoring.api_client')
    @mock.patch('bigflow.monitoring.increment_counter')
    @mock.patch('bigflow.monitoring.wait_for_metric')
    @mock.patch('bigflow.monitoring.api_create_metric')
    @mock.patch('bigflow.monitoring.metric_exists')
    def test_should_increment_counter(self,
                                      metric_exists_mock,
                                      api_create_metric_mock,
                                      wait_for_metric_mock,
                                      increment_counter_mock,
                                      api_client_mock):
        # given
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')
        api_client_mock.return_value = 'client'
        metric_exists_mock.return_value = True

        # when
        monitoring.increment_job_failure_count(monitoring_config, 'job id', logger)

        # then
        increment_counter_mock.assert_called_once_with(
            'client',
            monitoring_config,
            monitoring.BIGFLOW_JOB_FAILURE_METRIC_TYPE,
            'job id')


class FailingJob(object):
    def __init__(self, id):
        self.id = id

    def run(self, runtime):
        raise Exception('Panic!')


class MeterJobRunFailuresTestCase(TestCase):

    @mock.patch('bigflow.monitoring.increment_job_failure_count')
    def test_should_increment_failure_count_when_job_fail(self, increment_job_failure_count_mock):
        # given
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        metered_job = monitoring.meter_job_run_failures(FailingJob('job1'), monitoring_config)

        # when
        with self.assertRaises(Exception):
            metered_job.run('2019-01-01')

        # then
        increment_job_failure_count_mock.assert_called_once_with(monitoring_config, 'job1', logging.getLogger(monitoring.__name__))


class MeteredAndLoggedJobRunFailuresTestCase(TestCase):

    @mock.patch('bigflow.monitoring.increment_job_failure_count')
    @patch.object(GCPLogger, 'error')
    @mock.patch('bigflow.logger.create_logging_client')
    def test_should_increment_failure_count_and_call_gcp_logger(self, create_logging_client_mock, gcp_logger_mock, increment_job_failure_count_mock):
        # given
        create_logging_client_mock.return_value = mock.create_autospec(logging_v2.LoggingServiceV2Client)
        monitoring_config = monitoring.MonitoringConfig(
            'test project',
            'eu-west',
            'env')

        job_with_metrics_and_logs = log_job_run_failures(monitoring.meter_job_run_failures(FailingJob('job1'), monitoring_config), monitoring_config, 'workflow-id')

        # when
        with self.assertRaises(Exception):
            job_with_metrics_and_logs.run('2019-01-01')

        # then
        increment_job_failure_count_mock.assert_called_once_with(monitoring_config, 'job1',
                                                                 logging.getLogger(monitoring.__name__))

        # and
        gcp_logger_mock.assert_called_once_with("Panic!")
