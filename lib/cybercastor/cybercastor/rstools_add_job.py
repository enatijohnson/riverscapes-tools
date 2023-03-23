import traceback
import argparse
import time
import glob
from copy import copy
from pathlib import Path
import os
import sys
import json
import inquirer
from termcolor import colored
from rscommons import Logger, dotenv
# from rscommons.util import safe_remove_dir
from datetime import datetime

from cybercastor.lib import api
from cybercastor.lib.monitor import print_job, possible_states
from cybercastor.lib.cloudwatch import download_job_logs
from cybercastor.lib.rs_project_finder import find_projects

# All the master values for
# taskDefId and taskScriptId comes from here:
# https://cybercastor.northarrowresearch.com/engines/manifest.json


def get_params(job_obj):
    # We need to send in your .riverscapes file as a variable
    with open(os.path.join(Path.home(), '.riverscapes')) as f:
        rsconfig = json.load(f)
    program = job_obj['env']['PROGRAM']

    if program is None or len(program) == 0:
        raise Exception(
            'Could not find a valid PROGRAM environment variable in your job.')
    # to keep the env var short and sweet dump anything that's not this program
    rsconfig['default'] = program
    rsconfig['programs'] = {k: v for k,
                            v in rsconfig['programs'].items() if k == program}

    # Checking for sanity here and common sense errors if needed
    if len(rsconfig['programs']) == 0:
        raise Exception(
            'could not find program with key "{}" in your .riverscapes file.'.format(program))
    elif len(rsconfig['programs']) > 1:
        raise Exception('Found {} keys for program with key "{}" in your .riverscapes file. This can cause problems.'.format(
            len(rsconfig['programs']), program))

    # Job Environment variables are common to all tasks
    new_job_env = copy(job_obj['env']) if 'env' in job_obj else {}
    new_job_meta = copy(job_obj['meta']) if 'meta' in job_obj else {}

    # TODO: A little cheeky but in special cases we're going to suffix today's date on the job
    if 'TAGS' in new_job_env and new_job_env['TAGS'] == 'TEST_ALL':
        new_job_env['TAGS'] += ',{}'.format(
            datetime.now().strftime("%b%d").upper())

    new_job_env['RS_CONFIG'] = json.dumps(rsconfig)
    # NO_UI keeps the progress bars at bay value doesn't matter
    new_job_env['NO_UI'] = 'gnarly'

    # Our input JSON is huc-specific but the Cybercastor interface is generic
    def create_task(huc, resources=None):
        ret_obj = {
            "name": huc,  # Every Task needs a unique name so we can find it in the system
            # Task environment variables are unique to each task
            "env": json.dumps({'HUC': huc})
        }
        if resources is not None:
            ret_obj['taskDefProps'] = {
                "cpu": resources["cpu"],
                "memoryLimitMiB": resources["memory"],
                "ephemeralStorageGiB": resources["disk"]
            }
        return ret_obj

    # If resources exists we need to pull it out
    job_resources = job_obj["resources"] if "resources" in job_obj else None
    if job_resources is not None:
        if "cpu" not in job_resources or "memory" not in job_resources or "disk" not in job_resources:
            raise Exception(
                'If you use "resources" then all of "cpu", "memory" and "disk" are required')
        elif job_resources["cpu"] % 256 != 0 or job_resources["memory"] % 256 != 0:
            raise Exception('cpu and memory must be multiples of 256')
        elif job_resources["disk"] < 20 or job_resources["disk"] > 200:
            raise Exception('disk must be an integer between 20 and 200')

    params = {
        "job": {
            "name": job_obj['name'],
            "description": job_obj['description'],
            "taskDefId": "rstools",  # we're hardcoding the Docker machine to this
            "taskScriptId": job_obj['taskScriptId'],
            # Turn meta and env into stringified JSON which is what the API requires
            "meta": json.dumps(new_job_meta),
            "env": json.dumps(new_job_env)
        },
        "tasks": [create_task(x, job_resources) for x in job_obj['hucs']]
    }
    return params


def main(job_json_dir, api_url, username, password, rs_api_url: str) -> bool:
    job_choices = {}
    repeat = False
    monitor_logs_path = os.path.join(os.path.dirname(__file__), 'logs')
    for f in glob.glob(os.path.join(job_json_dir, '*.json')):
        fsplit = os.path.basename(f).split('.')
        if fsplit[0] not in job_choices:
            job_choices[fsplit[0]] = {"status": "Not Run"}
        if '.output.' in f:
            with open(f) as fi:
                try:
                    job_choices[fsplit[0]]["status"] = json.load(fi)['status']
                except Exception as e:
                    job_choices[fsplit[0]]["status"] = 'Unknown'
        else:
            with open(f) as fi:
                job_choices[fsplit[0]]["path"] = os.path.basename(f)
                try:
                    job_choices[fsplit[0]]["name"] = json.load(fi)['name']
                except Exception as e:
                    job_choices[fsplit[0]]["name"] = '???'

    jobs_sorted = sorted(job_choices.values(), key=lambda k: k['path'])

    questions = [
        inquirer.List('job',
                      message="Choose a Job:",
                      choices=[('[{}] {} </{}>'.format(jj['status'], jj['name'], jj['path']),
                                jj['path']) for jj in jobs_sorted] + [('Quit', 'quit')],
                      ),
    ]
    answers = inquirer.prompt(questions)

    if answers['job'] == 'quit':
        return False

    job_json = os.path.join(job_json_dir, answers['job'])
    # Load our JSON configuration file
    with open(job_json) as f:
        job_obj = json.load(f)

    # Initialize a connection to the riverscapes API
    data = find_projects(rs_api_url, job_obj)

    # Initialize our API and log in
    CybercastorAPI = api.CybercastorAPI(api_url, username, password)

    outputFile = os.path.splitext(job_json)[0] + '.output.json'

    # Check if we have an output file to read.
    # If we already have an output file then skip the job creation step and just continue through to monitoring
    job_monitor = None
    if os.path.isfile(outputFile):
        with open(outputFile) as f:
            try:
                job_monitor = json.load(f)
            except Exception as e:
                log.error(
                    'error parsing: {}. Recreating it \n\n{}'.format(outputFile, e))

    ##############################
    # Job Creation
    ##############################
    if job_monitor is None:
        # Make our params what the cybercastor  API needs
        params = get_params(job_obj)

        with open(outputFile, 'w') as outfile:
            # Add the job to the API
            result = CybercastorAPI.add_job(params)
            if result is None:
                raise Exception('Error')
            json.dump(result, outfile, indent=4, sort_keys=True)
            job_monitor = result

    ##############################
    # Monitoring
    ##############################
    # Open the results of the addition step above and print
    # the status until everything is done

    # Now start a job loop
    while True:
        # Make an API query for the job that is in the output json file
        job_monitor = CybercastorAPI.get_job(job_monitor['id'])
        # Immediately write the new state to the file
        with open(outputFile, 'w') as outfile:
            job_monitor['meta'] = json.loads(job_monitor['meta'])
            job_monitor['env'] = json.loads(job_monitor['env'])
            if 'RS_CONFIG' in job_monitor['env']:
                # don't show login info in the file
                del job_monitor['env']['RS_CONFIG']
            for t in job_monitor['tasks']:
                t['env'] = json.loads(t['env'])
                t['meta'] = json.loads(t['meta'])
            json.dump(job_monitor, outfile, indent=4, sort_keys=True)

        # Clear the screen and start printing our report
        print(chr(27) + "[2J")
        print_job(job_monitor)

        menu_choice = inquirer.list_input(
            message="Actions:",
            choices=[
                ('Reload', 'reload'),
                ('Download Logs', 'download_logs'),
                ('Manage Tasks', 'task_manage'),
                ('Back to Jobs', 'menu_back'),
                ('Quit', 'quit')
            ],
            default=['reload']
        )

        if menu_choice == 'quit':
            break

        elif menu_choice == 'menu_back':
            repeat = True
            break

        elif menu_choice == 'download_logs':
            download_job_logs(job_monitor, monitor_logs_path,
                              download_running=True)
            log.info('DONE!')
            time.sleep(3)
        elif menu_choice == 'task_manage':
            # Get a fresh copy to work with
            manage_tasks(CybercastorAPI, job_monitor['id'])

    print('Goodbye!!')
    return repeat


def manage_tasks(CybercastorAPI, jid):
    """_summary_

    Args:
        CybercastorAPI (_type_): _description_
        jid (_type_): _description_

    Returns:
        _type_: _description_
    """
    # Get a fresh version
    job_monitor = CybercastorAPI.get_job(jid)
    total_jobs = len(job_monitor['tasks'])
    ts = {
        'restartable': [t for t in job_monitor['tasks'] if t['status'] in ['STOPPED', 'SUCCEEDED', 'FAILED']],
        'stoppable': [t for t in job_monitor['tasks'] if t['status'] in ['QUEUED', 'RUNNING']],
        'failed': [t for t in job_monitor['tasks'] if t['status'] in ['FAILED']],
        'stopped': [t for t in job_monitor['tasks'] if t['status'] in ['STOPPED']]
    }
    change_state = {
        'restart_failed': 'failed',
        'restart_stopped': 'stopped',
        'restart_all': 'restartable',
        'stop_all': 'stoppable'
    }

    menu_choice = inquirer.list_input(
        message="Choose?",
        choices=[
            ('Restart tasks from list ({}/{} available)'.format(
                len(ts['restartable']), total_jobs), 'restart_tasks'),
            ('Restart FAILED tasks ({}/{} available)'.format(
                len(ts['failed']), total_jobs), 'restart_failed'),
            ('Restart STOPPED tasks ({}/{} available)'.format(
                len(ts['stopped']), total_jobs), 'restart_stopped'),
            ('Restart ALL {} Restartable tasks (Stopped, Succeeded and Failed)'.format(
                len(ts['restartable'])), 'restart_all'),
            ('Stop tasks from list ({}/{} available)'.format(
                len(ts['stoppable']), total_jobs), 'stop_tasks'),
            ('Stop ALL {} Running/Queued tasks'.format(
                len(ts['stoppable'])), 'stop_all'),
            ('<== Menu Back', 'back')
        ],
        default=['reload']
    )

    # Tasks from list Chooser
    if menu_choice == 'restart_tasks' or menu_choice == 'stop_tasks':
        if menu_choice == 'restart_tasks':
            op_text = 'restart'
            dict_key = 'restartable'
            op = VALID_OPS['START']
        else:
            op_text = 'stop'
            dict_key = 'stoppable'
            op = VALID_OPS['stop']
        questions = [
            inquirer.Checkbox('tasks',
                              message="Which tasks to {}? <space> to select. <enter> to approve".format(
                                  op_text),
                              choices=[
                                  (colored('{} ({})'.format(
                                      t['name'], t['status']), possible_states[t['status']]), t)
                                  for t in ts[dict_key]
                              ]
                              )
        ]
        answers = inquirer.prompt(questions)['tasks']
        if (len(answers) == 0) or not inquirer.confirm('This will restart {} tasks:'.format(len(answers))):
            return
        return change_task_status(CybercastorAPI, answers, op)

    # Affected all tasks of a certain type
    elif menu_choice in change_state.keys():
        if 'restart' in menu_choice:
            op_text = 'restart'
            op = VALID_OPS['START']
        else:
            op_text = 'stop'
            op = VALID_OPS['STOP']

        dict_key = change_state[menu_choice]
        num_available_tasks = len(ts[dict_key])

        if num_available_tasks == 0:
            return

        if inquirer.confirm('This will {} {} {} tasks:'.format(op_text, dict_key, num_available_tasks)):
            return change_task_status(CybercastorAPI, ts[dict_key], op)
        return

    print('DONE')


VALID_OPS = {'START': 'START', 'STOP': 'STOP'}


def change_task_status(CybercastorAPI, tasks, op):
    """ Change the status of a task

    Arguments:
        CybercastorAPI {[type]} -- [description]
        tasks {[type]} -- [description]
        op {[type]} -- [description]
    """
    log = Logger('change_task_status')
    if op not in VALID_OPS.values():
        raise Exception('op not found')

    counter = 0
    for t in tasks:
        counter += 1
        try:
            if op == VALID_OPS['START']:
                CybercastorAPI.start_task(t['id'])
            elif op == VALID_OPS['STOP']:
                CybercastorAPI.stop_task(t['id'])
            log.info('   -Completed {} on task: {}  ({}/{})'.format(op,
                                                                    t['name'], counter, len(tasks)))
        except Exception as e:
            log.error('   -Error {} on task: {}  ({}/{})'.format(op,
                                                                 t['name'], counter, len(tasks)))
            log.error(e)
    log.info('Pausing for 10 seconds')
    time.sleep(10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # parser.add_argument('hucs_json', help='JSON with array of HUCS', type=str)
    parser.add_argument(
        'job_json', help='The job specification JSON file', type=str)
    parser.add_argument('api_url', help='URL to the cybercastor API', type=str)
    parser.add_argument('username', help='API URL Username', type=str)
    parser.add_argument('password', help='API URL Password', type=str)
    parser.add_argument('rs_api_url', help='URL to the Riverscapes API', type=str)
    parser.add_argument('--verbose', help='(optional) a little extra logging ',
                        action='store_true', default=False)

    args = dotenv.parse_args_env(parser, os.path.join(
        os.path.dirname(__file__), '..', '.env.python'))

    # Initiate the log file
    log = Logger("CCAddJob")
    log.setup(verbose=args.verbose)
    log.title('Cybercastor Add JOB')

    # Stupid slash parsing
    fixedurl = args.api_url.replace(':/', '://')

    try:
        RETRY = True
        while RETRY is True:
            RETRY = main(args.job_json, fixedurl, args.username, args.password,args.rs_api_url)

    except Exception as e:
        log.error(e)
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)

    sys.exit(0)
