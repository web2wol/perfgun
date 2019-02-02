#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import csv
from difflib import SequenceMatcher

import traceback
from time import time
import sys
import warnings
import requests
import contextlib

import re
from jira import JIRA
import hashlib
import yaml

from reportportal_client import ReportPortalServiceAsync
from functools import partial

UNDEFINED = "undefined"

FIELDNAMES = 'action', 'simulation', 'thread', "simulation_name", "request_name", \
             "request_start", "request_end", "status", "gatling_error", "error"

ERROR_FIELDS = 'Response', 'Request_params', 'Gatling_error'

PATH_TO_CONFIG = "/tmp/config.yaml"


class partialmethod(partial):
    def __get__(self, instance, owner):
        if instance is None:
            return self

        return partial(self.func, instance, *(self.args or ()),
                       **(self.keywords or {}))


class SimulationLogParser(object):
    def __init__(self, arguments):
        self.args = arguments

    def parse_log(self):
        """Parse line with error and send to database"""
        simulation = None
        path = args['file']
        unparsed_counter = 0
        errors = {}
        with open(path) as tsv:
            for entry in csv.DictReader(tsv, delimiter="\t", fieldnames=FIELDNAMES, restval="not_found"):
                if simulation is None:
                    simulation = entry['simulation_name']
                if len(entry) >= 8 and (entry['status'] == "KO"):
                    try:
                        data = self.parse_entry(entry)
                        data['simulation'] = simulation
                        data['environment'] = self.args['environment']
                        data["request_params"] = self.remove_session_id(data["request_params"])
                        count = 1
                        key = "%s_%s_%s" % (data['request_name'], data['error_code'], data['response_code'])
                        if key not in errors:
                            errors[key] = {"Request name": data['request_name'], "Method": data['request_method'],
                                           "Request headers": data["headers"], 'Error count': count,
                                           'Environment': data['environment'],
                                           "Response code": data['response_code'], "Error code": data['error_code'],
                                           "Request URL": data['request_url'], "Request_params": [], "Response": [],
                                           "Gatling_error": []}
                        for field in ERROR_FIELDS:
                            same = self.check_dublicate(errors[key], data, field)
                            if same is True:
                                errors[key]['Error count'] += 1
                                break
                            else:
                                errors[key][field].append(data[field.lower()])
                    except:
                        unparsed_counter += 1
                        pass

        if unparsed_counter > 0:
            print("Unparsed errors: %d" % unparsed_counter)
        return errors

    def check_dublicate(self, entry, data, field):
        for params in entry[field]:
            if SequenceMatcher(None, str(data[field.lower()]), str(params)).ratio() > 0.7:
                return True

    def parse_entry(self, values):
        """Parse error entry"""
        values['test_type'] = self.args['type']
        values['user_count'] = self.args['count']
        values['response_time'] = int(values['request_end']) - int(values['request_start'])
        values['request_start'] += "000000"
        values['response_code'] = self.extract_response_code(values['error'])
        values['error_code'] = self.extract_error_code(values['error'])
        values['request_url'], values['request_params'], values['request_method'] = self.parse_request(values['error'])
        values['headers'] = self.escape_for_json(self.parse_headers(values['error']))
        values['response'] = self.parse_response(values['error'])
        values['error'] = self.escape_for_json(values['error'])
        values['gatling_error'] = self.escape_for_json(values['gatling_error'])

        return values

    def extract_error_code(self, error_code):
        """Extract code of error from response body"""
        error_code_regex = re.search(r'("code"|"Code"): ?"?(-?\d+)"?,', error_code)
        if error_code_regex and error_code_regex.group(2):
            return error_code_regex.group(2)
        return UNDEFINED

    def remove_session_id(self, param):
        sessionid_regex = re.search(r'(SessionId|sessionID|sessionId|SessionID)=(.*?&|.*)', param)
        if sessionid_regex and sessionid_regex.group(2):
            return param.replace(sessionid_regex.group(2), '_...')
        return param

    def extract_response_code(self, error):
        """Extract response code"""
        code_regexp = re.search(r"HTTP Code: ?([a-zA-Z]*?\(?(\d+)\)?),", error)
        if code_regexp and code_regexp.group(2):
            return code_regexp.group(2)
        return UNDEFINED

    def escape_for_json(self, string):
        if isinstance(string, str):
            return string.replace('"', '&quot;') \
                .replace("\\", "&#92;") \
                .replace("/", "&#47;") \
                .replace("<", "&lt;") \
                .replace(">", "&gt;")
        return string

    def parse_request(self, param):
        regex = re.search(r"Request: ?(.+?) ", param)
        if regex and regex.group(1):
            request_parts = regex.group(1).split("?")
            url = request_parts[0]
            params = request_parts[len(request_parts) - 1] if len(request_parts) >= 2 else ''
            url = self.escape_for_json(url)
            params = self.escape_for_json(params)
            method = re.search(r" ([A-Z]+) headers", param).group(1)
            return url, params, method
        return UNDEFINED, UNDEFINED, UNDEFINED

    def parse_headers(self, param):
        regex = re.search(r"headers: ?(.+?) ?,", param)
        if regex and regex.group(1):
            return regex.group(1)
        return UNDEFINED

    def parse_response(self, param):
        regex = re.search(r"Response: ?(.+)$", param)
        if regex and regex.group(1):
            return self.escape_for_json(regex.group(1))
        return None


class ReportPortal:
    def __init__(self, errors_data, arguments, rp_url, rp_token, rp_project):
        self.errors = errors_data
        self.args = arguments
        self.rp_url = rp_url
        self.rp_token = rp_token
        self.rp_project = rp_project

    @contextlib.contextmanager
    def no_ssl_verification(self):
        old_request = requests.Session.request
        requests.Session.request = partialmethod(old_request, verify=False)

        warnings.filterwarnings('ignore', 'Unverified HTTPS request')
        yield
        warnings.resetwarnings()

        requests.Session.request = old_request

    def timestamp(self):
        return str(int(time() * 1000))

    def create_project(self):
        headers = {'authorization': 'bearer ' + self.rp_token}
        post_data = {'entryType': 'INTERNAL', 'projectName': self.rp_project}
        r = requests.get(self.rp_url + '/' + self.rp_project, headers=headers)
        if r.status_code == 404 or r.text.find(self.rp_project) == -1:
            p = requests.post(self.rp_url, json=post_data, headers=headers)

    def my_error_handler(self, exc_info):
        """
        This callback function will be called by async service client when error occurs.
        Return True if error is not critical and you want to continue work.
        :param exc_info: result of sys.exc_info() -> (type, value, traceback)
        :return:
        """
        print("Error occurred: {}".format(exc_info[1]))
        traceback.print_exception(*exc_info)

    def html_decode(self, s):
        html_codes = (
            ("'", '&#39;'),
            ("/", '&#47;'),
            ('"', '&quot;'),
            (':', '%3A'),
            ('/', '%2F'),
            ('.', '%2E'),
            ('&', '&amp;'),
            ('>', '&gt;'),
            ('|', '%7C'),
            ('<', '&lt;'),
            ('\\"', '"')
        )
        for code in html_codes:
            s = s.replace(code[1], code[0])
        return s

    def log_message(self, service, message, errors, level='WARN'):
        if errors[message] is not 'undefined':
            if isinstance(errors[message], list):
                if len(errors[message]) > 1:
                    log = ''
                    for i, error in enumerate(errors[message]):
                        log += message + ' ' + str(i + 1) + ': ' + error + ';;\n'
                    service.log(time=self.timestamp(),
                                message="{}".format(self.html_decode(log)),
                                level="{}".format(level))
                elif not str(errors[message])[2:-2].__contains__('undefined'):
                    service.log(time=self.timestamp(),
                                message="{}: {}".format(message, self.html_decode(str(errors[message])[2:-2])),
                                level="{}".format(level))
            else:
                service.log(time=self.timestamp(),
                            message="{}: {}".format(message, self.html_decode(str(errors[message]))),
                            level="{}".format(level))

    def log_unique_error_id(self, service, request_name, method, response_code, error_code):
        error_id = ""
        if method is not 'undefined':
            error_id += method + '_' + request_name
        else:
            error_id += request_name
        if response_code is not 'undefined':
            error_id += '_' + response_code
        elif error_code is not 'undefined':
            error_id += '_' + error_code
        service.log(time=self.timestamp(), message=error_id, level='ERROR')

    def get_item_name(self, entry):
        if entry['Method'] is not 'undefined' and entry['Response code'] is not 'undefined':
            return "{} {} {}".format(str(entry['Request name']),
                                     str(entry['Method']),
                                     str(entry['Response code']))
        else:
            return str(entry['Request name'])

    def report_errors(self):
        with self.no_ssl_verification():
            self.create_project()
            service = ReportPortalServiceAsync(endpoint=self.rp_url, project=self.rp_project,
                                               token=self.rp_token, error_handler=self.my_error_handler)

            errors = self.errors
            errors_len = len(errors)
            if errors_len > 0:
                # Start launch.
                service.start_launch(name=self.args['type'],
                                     start_time=self.timestamp(),
                                     description='This simulation has {} fails'.format(errors_len))
                for key in errors:
                    # Start test item.
                    item_name = self.get_item_name(errors[key])
                    service.start_test_item(name=item_name,
                                            description="This request was failed {} times".format(
                                                errors[key]['Error count']),
                                            tags=[self.args['type'], self.args['url'], 'gatling_test'],
                                            start_time=self.timestamp(),
                                            item_type="STEP",
                                            parameters={"simulation": self.args['simulation'],
                                                        'duration': self.args['duration'],
                                                        'user count': self.args['count'],
                                                        'rump duration': self.args['rumpup'],
                                                        'environment': self.args['environment'],
                                                        'test type': self.args['type']})

                    self.log_message(service, 'Request name', errors[key], 'WARN')
                    self.log_message(service, 'Method', errors[key], 'WARN')
                    self.log_message(service, 'Request URL', errors[key], 'WARN')
                    self.log_message(service, 'Request_params', errors[key], 'WARN')
                    self.log_message(service, 'Request headers', errors[key], 'INFO')
                    self.log_message(service, 'Environment', errors[key], 'INFO')
                    self.log_message(service, 'Error count', errors[key], 'WARN')
                    self.log_message(service, 'Error code', errors[key], 'WARN')
                    self.log_message(service, 'Gatling_error', errors[key], 'WARN')
                    self.log_message(service, 'Response code', errors[key], 'WARN')
                    self.log_message(service, 'Response', errors[key], 'WARN')
                    self.log_unique_error_id(service, errors[key]['Request name'], errors[key]['Method'],
                                             errors[key]['Response code'], errors[key]['Error code'])

                    service.finish_test_item(end_time=self.timestamp(), status="FAILED")
            else:
                service.start_launch(name=self.args['type'],
                                     start_time=self.timestamp(),
                                     description='This simulation has no fails')

            # Finish launch.
            service.finish_launch(end_time=self.timestamp())

            service.terminate()


class JiraWrapper:
    JIRA_REQUEST = 'project={} AND labels in ({})'

    def __init__(self, url, user, password, jira_project, assignee, issue_type='Bug', labels=None, watchers=None,
                 jira_epic_key=None):
        self.valid = True
        self.url = url
        self.password = password
        self.user = user
        try:
            self.connect()
        except:
            self.valid = False
            return
        self.projects = [project.key for project in self.client.projects()]
        self.project = jira_project
        if self.project not in self.projects:
            self.client.close()
            self.valid = False
            return
        self.assignee = assignee
        self.issue_type = issue_type
        self.labels = list()
        if labels:
            self.labels = [label.strip() for label in labels.split(",")]
        self.watchers = list()
        if watchers:
            self.watchers = [watcher.strip() for watcher in watchers.split(",")]
        self.jira_epic_key = jira_epic_key
        self.client.close()

    def connect(self):
        self.client = JIRA(self.url, basic_auth=(self.user, self.password))

    def markdown_to_jira_markdown(self, content):
        return content.replace("###", "h3.").replace("**", "*")

    def create_issue(self, title, priority, description, issue_hash, attachments=None, get_or_create=True,
                     additional_labels=None):
        description = self.markdown_to_jira_markdown(description)
        _labels = [issue_hash]
        if additional_labels and isinstance(additional_labels, list):
            _labels.extend(additional_labels)
        _labels.extend(self.labels)
        issue_data = {
            'project': {'key': self.project},
            'summary': re.sub('[^A-Za-z0-9//\. _]+', '', title),
            'description': description,
            'issuetype': {'name': self.issue_type},
            'assignee': {'name': self.assignee},
            'priority': {'name': priority},
            'labels': _labels
        }
        jira_request = self.JIRA_REQUEST.format(issue_data["project"]["key"], issue_hash)
        if get_or_create:
            issue, created = self.get_or_create_issue(jira_request, issue_data)
        else:
            issue = self.post_issue(issue_data)
            created = True
        if attachments:
            for attachment in attachments:
                if 'binary_content' in attachment:
                    self.add_attachment(issue.key,
                                        attachment=attachment['binary_content'],
                                        filename=attachment['message'])
        for watcher in self.watchers:
            self.client.add_watcher(issue.id, watcher)
        if self.jira_epic_key:
            self.client.add_issues_to_epic(self.jira_epic_key, [issue.id])
        return issue, created

    def add_attachment(self, issue_key, attachment, filename=None):
        issue = self.client.issue(issue_key)
        for _ in issue.fields.attachment:
            if _.filename == filename:
                return
        self.client.add_attachment(issue, attachment, filename)

    def post_issue(self, issue_data):
        print(issue_data)
        issue = self.client.create_issue(fields=issue_data)
        return issue

    def get_or_create_issue(self, search_string, issue_data):
        issuetype = issue_data['issuetype']['name']
        created = False
        jira_results = self.client.search_issues(search_string)
        issues = []
        for each in jira_results:
            if each.fields.summary == issue_data.get('summary', None):
                issues.append(each)
        if len(issues) == 1:
            issue = issues[0]
            if len(issues) > 1:
                print('  more then 1 issue with the same summary')
            else:
                print(issuetype + 'issue already exists:' + issue.key)
        else:
            issue = self.post_issue(issue_data)
            created = True
        return issue, created


def create_description(error, arguments):
    description = ""
    if arguments['simulation']:
        description += "Simulation: " + arguments['simulation'] + "\n"
    if arguments['url']:
        description += "Target environment: " + arguments['url'] + "\n"
    if error['Request URL']:
        description += "Request URL: " + error['Request URL'] + "\n"
    if error['Request_params']:
        description += "Request params: " + str(error['Request_params'])[2:-2] + "\n"
    if error['Gatling_error']:
        description += "Gatling error: " + str(error['Gatling_error']) + "\n"
    if error['Error count']:
        description += "Error count: " + str(error['Error count']) + "\n"
    if error['Response code']:
        description += "Response code: " + error['Response code'] + "\n"

    return description


def finding_error_string(error, arguments):
    error_str = arguments['simulation'] + "_" + arguments['url'] + "_" + str(error['Gatling_error']) + "_" \
                    + error['Request name']
    return error_str


def get_hash_code(error, arguments):
    hash_string = finding_error_string(error, arguments).strip()
    return hashlib.sha256(hash_string.encode('utf-8')).hexdigest()


def get_args():
    parser = argparse.ArgumentParser(description='Simlog parser.')
    parser.add_argument("-f", "--file", help="file path", required=True, default=None)
    parser.add_argument("-c", "--count", type=int, required=True, help="User count.")
    parser.add_argument("-t", "--type", required=True, help="Test type.")
    parser.add_argument("-e", "--environment", help='Target environment', default=None)
    parser.add_argument("-d", "--duration", help='Test duration', default=None)
    parser.add_argument("-r", "--rumpup", help='Rump up time', default=None)
    parser.add_argument("-u", "--url", help='Environment url', default=None)
    parser.add_argument("-s", "--simulation", help='Test simulation', default=None)  # should be the same as on Grafana
    return vars(parser.parse_args())


def report_errors(errors, args):
    report_types = []
    with open(PATH_TO_CONFIG, "rb") as f:
        config = yaml.load(f.read())
    if config:
        report_types = list(config.keys())

    rp_service = None
    if report_types.__contains__('reportportal'):
        rp_project = config['reportportal'].get("rp_project_name")
        rp_url = config['reportportal'].get("rp_host")
        rp_token = config['reportportal'].get("rp_token")
        if not (rp_project and rp_url and rp_token):
            print("ReportPortal configuration values missing, proceeding "
                  "without report portal integration ")
        else:
            rp_service = ReportPortal(errors, args, rp_url, rp_token, rp_project)
    if rp_service:
        rp_service.my_error_handler(sys.exc_info())
        rp_service.report_errors()

    jira_service = None
    if report_types.__contains__('jira'):
        jira_url = config['jira'].get("url", None)
        jira_user = config['jira'].get("username", None)
        jira_pwd = config['jira'].get("password", None)
        jira_project = config['jira'].get("jira_project", None)
        jira_assignee = config['jira'].get("assignee", None)
        jira_issue_type = config['jira'].get("issue_type", 'Bug')
        jira_lables = config['jira'].get("labels", '')
        jira_watchers = config['jira'].get("watchers", '')
        jira_epic_key = config['jira'].get("epic_link", None)
        if not (jira_url and jira_user and jira_pwd and jira_project and jira_assignee):
            print("Jira integration configuration is messed up, proceeding without Jira")
        else:
            jira_service = JiraWrapper(jira_url, jira_user, jira_pwd, jira_project,
                                       jira_assignee, jira_issue_type, jira_lables,
                                       jira_watchers, jira_epic_key)
    if jira_service:
        jira_service.connect()
        if jira_service.valid:
            for error in errors:
                issue_hash = get_hash_code(errors[error], args)
                description = create_description(errors[error], args)
                jira_service.create_issue(errors[error]['Request name'], 'Major', description, issue_hash)
        else:
            print("Something wrong with Jira")


if __name__ == '__main__':
    print("Parsing simulation log")
    args = get_args()
    logParser = SimulationLogParser(args)
    errors = logParser.parse_log()
    report_errors(errors, args)