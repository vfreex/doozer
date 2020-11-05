"""
Utility functions for general interactions with Brew and Builds
"""
from __future__ import absolute_import, print_function, unicode_literals
from logging import log

# stdlib
import time
import subprocess
from multiprocessing import Lock
import traceback
import requests
from typing import List, Tuple, Dict, Optional

from . import logutil

# 3rd party
import koji
import koji_cli.lib
import wrapt


logger = logutil.getLogger(__name__)

# ============================================================================
# Brew/Koji service interaction functions
# ============================================================================

# Populated by watch_task. Each task_id will be a key in the dict and
# each value will be a TaskInfo: https://github.com/openshift/enterprise-images/pull/178#discussion_r173812940
watch_task_info = {}
# Protects threaded access to watch_task_info
watch_task_lock = Lock()


def get_watch_task_info_copy():
    """
    :return: Returns a copy of the watch_task info dict in a thread safe way. Each key in this dict
     is a task_id and each value is a koji TaskInfo with potentially useful data.
     https://github.com/openshift/enterprise-images/pull/178#discussion_r173812940
    """
    with watch_task_lock:
        return dict(watch_task_info)


def watch_task(brew_hub, log_f, task_id, terminate_event):
    end = time.time() + 4 * 60 * 60
    watcher = koji_cli.lib.TaskWatcher(
        task_id,
        koji.ClientSession(brew_hub, opts={'serverca': '/etc/pki/brew/legacy.crt'}),
        quiet=True)
    error = None
    except_count = 0
    while error is None:
        try:
            watcher.update()
            except_count = 0

            # Keep around metrics for each task we watch
            with watch_task_lock:
                watch_task_info[task_id] = dict(watcher.info)

            if watcher.is_done():
                return None if watcher.is_success() else watcher.get_failure()
            log_f("Task state: " + koji.TASK_STATES[watcher.info['state']])
        except:
            except_count += 1
            # possible for watcher.update() to except during connection issue, try again
            log_f('watcher.update() exception. Trying again in 60s.\n{}'.format(traceback.format_exc()))
            if except_count >= 10:
                log_f('watcher.update() excepted 10 times. Giving up.')
                error = traceback.format_exc()
                break

        if terminate_event.wait(timeout=3 * 60):
            error = 'Interrupted'
        elif time.time() > end:
            error = 'Timeout building image'

    log_f(error + ", canceling build")
    subprocess.check_call(("brew", "cancel", str(task_id)))
    return error


def watch_tasks(brew_hub, log_f, task_ids, terminate_event):
    if not task_ids:
        return
    end = time.time() + 4 * 60 * 60
    session = koji.ClientSession(brew_hub, opts={'serverca': '/etc/pki/brew/legacy.crt'})
    watchers = {}
    errors = {}
    except_counts = {}
    for task_id in task_ids:
        watchers[task_id] = koji_cli.lib.TaskWatcher(task_id, session, quiet=True)
        except_counts[task_id] = 0
    tasks_to_poll = set(watchers.keys())
    tasks_to_cancel = set()
    while True:
        for task_id in tasks_to_poll.copy():
            watcher = watchers[task_id]
            try:
                watcher.update()
                except_counts[task_id] = 0
                # Keep around metrics for each task we watch
                with watch_task_lock:
                    watch_task_info[task_id] = dict(watcher.info)
                if watcher.is_done():
                    errors[task_id] = None if watcher.is_success() else watcher.get_failure()
                    tasks_to_poll.remove(task_id)
                log_f(f"Task {task_id} state: {koji.TASK_STATES[watcher.info['state']]}")
            except:
                except_counts[task_id] += 1
                # possible for watcher.update() to except during connection issue, try again
                log_f('watcher.update() exception. Trying again in 60s.\n{}'.format(traceback.format_exc()))
                if except_counts[task_id] >= 10:
                    log_f('watcher.update() excepted 10 times. Giving up.')
                    errors[task_id] = traceback.format_exc()
                    tasks_to_cancel.add(task_id)
                    tasks_to_poll.remove(task_id)
        if not tasks_to_poll:
            break
        if terminate_event.wait(timeout=3 * 60):
            for task_id in tasks_to_poll:
                tasks_to_cancel.add(task_id)
                errors[task_id] = 'Interrupted'
            break
        if time.time() > end:
            for task_id in tasks_to_poll:
                tasks_to_cancel.add(task_id)
                errors[task_id] = 'Timeout watching task'
            break
    if tasks_to_cancel:
        log_f(errors + ", canceling builds")
        for task_id in tasks_to_poll:
            log_f(f"Error waiting for Brew task {task_id}: {errors[task_id]}. Canceling...")
            canceled = session.cancelTask(task_id, recurse=True)
            if canceled:
                log_f(f"Brew task {task_id} was canceled.")
            else:
                log_f(f"Brew task {task_id} was NOT canceled.")
    return errors


def get_build_objects(ids_or_nvrs, session):
    """Get information of multiple Koji/Brew builds

    :param ids_or_nvrs: list of build nvr strings or numbers.
    :param session: instance of :class:`koji.ClientSession`
    :return: a list Koji/Brew build objects
    """
    logger.debug(
        "Fetching build info for {} from Koji/Brew...".format(ids_or_nvrs))
    # Use Koji multicall interface to boost performance. See https://pagure.io/koji/pull-request/957
    tasks = []
    with session.multicall(strict=True) as m:
        for b in ids_or_nvrs:
            tasks.append(m.getBuild(b))
    return [task.result for task in tasks]


def get_latest_builds(tag_component_tuples: List[Tuple[str, str]], build_type: Optional[str], event: Optional[int], session: koji.ClientSession) -> List[Optional[List[Dict]]]:
    """ Get latest builds for multiple Brew components as of given event

    :param tag_component_tuples: List of (tag, component_name) tuples
    :param build_type: if given, only retrieve specified build type (rpm, image)
    :param event: Brew event ID, or None for now.
    :param session: instance of Brew session
    :return: a list of lists of Koji/Brew build dicts
    """
    tasks = []
    with session.multicall(strict=True) as m:
        for tag, component_name in tag_component_tuples:
            if not tag:
                tasks.append(None)
                continue
            tasks.append(m.getLatestBuilds(tag, event=event, package=component_name, type=build_type))
    return [task.result if task else None for task in tasks]


def get_tagged_builds(tags: List[str], build_type: Optional[str], event: Optional[int], session: koji.ClientSession) -> List[Optional[List[Dict]]]:
    """ Get tagged builds for multiple Brew tags

    :param tag_component_tuples: List of (tag, component_name) tuples
    :param build_type: if given, only retrieve specified build type (rpm, image)
    :param event: Brew event ID, or None for now.
    :param session: instance of Brew session
    :return: a list of lists of Koji/Brew build dicts
    """
    tasks = []
    with session.multicall(strict=True) as m:
        for tag in tags:
            tasks.append(m.listTagged(tag, event=event, type=build_type))
    return [task.result if task else None for task in tasks]


def list_archives_by_builds(build_ids: List[int], build_type: str, session: koji.ClientSession) -> List[Optional[List[Dict]]]:
    """ Retrieve information about archives by builds
    :param build_ids: List of build IDs
    :param build_type: build type, such as "image"
    :param session: instance of Brew session
    :return: a list of Koji/Brew archive lists (augmented with "rpms" entries for RPM lists)
    """
    tasks = []
    with session.multicall(strict=True) as m:
        for build_id in build_ids:
            if not build_id:
                tasks.append(None)
                continue
            tasks.append(m.listArchives(buildID=build_id, type=build_type))
    archives_list = [task.result if task else None for task in tasks]

    # each archives record contains an archive per arch; look up RPMs for each
    archives = [ar for rec in archives_list for ar in rec or []]
    archives_rpms = list_image_rpms([ar["id"] for ar in archives], session)
    for archive, rpms in zip(archives, archives_rpms):
        archive["rpms"] = rpms

    return archives_list


def get_builds_tags(build_nvrs, session=None):
    """Get tags of multiple Koji/Brew builds

    :param builds_nvrs: list of build nvr strings or numbers.
    :param session: instance of :class:`koji.ClientSession`
    :return: a list of Koji/Brew tag lists
    """
    tasks = []
    with session.multicall(strict=True) as m:
        for nvr in build_nvrs:
            tasks.append(m.listTags(build=nvr))
    return [task.result for task in tasks]


def list_image_rpms(image_ids: List[int], session: koji.ClientSession) -> List[Optional[List[Dict]]]:
    """ Retrieve RPMs in given images
    :param image_ids: image IDs list
    :param session: instance of Brew session
    :return: a list of Koji/Brew RPM lists
    """
    tasks = []
    with session.multicall(strict=True) as m:
        tasks = [m.listRPMs(imageID=image_id) for image_id in image_ids]
    return [task.result for task in tasks]


def list_build_rpms(build_ids: List[int], session: koji.ClientSession) -> List[Optional[List[Dict]]]:
    """ Retrieve RPMs in given package builds (not images)
    :param build_ids: list of build IDs
    :param session: instance of Brew session
    :return: a list of Koji/Brew RPM lists
    """
    tasks = []
    with session.multicall(strict=True) as m:
        tasks = [m.listBuildRPMs(build) for build in build_ids]
    return [task.result for task in tasks]


# Map that records the most recent change for a tag.
# Maps tag_id to a list containing the most recent change
# event returned by koji's tagHistory API.
latest_tag_change_cache = {}
cache_lock = Lock()


def tags_changed_since_build(runtime, koji_client, build, tag_ids):
    """
    :param build:  A build information dict returned from koji getBuild
    :param tag_ids: A list of tag ids (or tag names) which should be assessed
    :return: If any of the tags have changed since the specified event, returns a
                list of information about the tags. If no tags have changed since
                event, returns an empty list
    """
    build_nvr = build['nvr']
    build_event_id = build['creation_event_id']

    result = []
    for tid in tag_ids:
        with cache_lock:
            tag_changes = latest_tag_change_cache.get(tid, None)

        if tag_changes is None:
            # koji returns in reverse chronological order. So we are retrieving the most recent.
            tag_changes = koji_client.tagHistory(tag=tid, queryOpts={'limit': 1})
            with cache_lock:
                latest_tag_change_cache[tid] = tag_changes

        if tag_changes:
            tag_change = tag_changes[0]
            tag_change_event_id = tag_change['create_event']
            if tag_change_event_id > build_event_id:
                result.append(tag_change)

    runtime.logger.debug(f'Found that build of {build_nvr} (event={build_event_id}) occurred before tag changes: {result}')
    return result


class KojiWrapper(wrapt.ObjectProxy):
    """
    We've see the koji client occasionally get
    Connection Reset by Peer errors.. "requests.exceptions.ConnectionError: ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
    Under the theory that these operations just need to be retried,
    this wrapper will automatically retry all invocations of koji APIs.
    """

    def __call__(self, *args, **kwargs):
        retries = 4
        while retries > 0:
            try:
                return self.__wrapped__(*args, **kwargs)
            except requests.exceptions.ConnectionError as ce:
                time.sleep(5)
                retries -= 1
                if retries == 0:
                    raise ce
