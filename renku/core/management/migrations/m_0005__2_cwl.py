# -*- coding: utf-8 -*-
#
# Copyright 2020 - Swiss Data Science Center (SDSC)
# A partnership between École Polytechnique Fédérale de Lausanne (EPFL) and
# Eidgenössische Technische Hochschule Zürich (ETHZ).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Initial migrations."""

import glob
import os
import uuid
from pathlib import Path

from cwlgen import CommandLineTool, parse_cwl
from cwlgen.requirements import InitialWorkDirRequirement
from git import NULL_TREE, Actor
from werkzeug.utils import secure_filename

from renku.core.models.entities import Collection, Entity
from renku.core.models.locals import with_reference
from renku.core.models.provenance.activities import ProcessRun, WorkflowRun
from renku.core.models.workflow.parameters import CommandArgument, \
    CommandInput, CommandOutput, MappedIOStream
from renku.core.models.workflow.run import Run
from renku.version import __version__, version_url


def migrate(client):
    """Migration function."""
    _migrate_old_workflows(client)


def _migrate_old_workflows(client):
    """Migrates old cwl workflows to new jsonld format."""
    wf_path = '{}/*.cwl'.format(client.workflow_path)

    cwl_paths = glob.glob(wf_path)

    cwl_paths = [(p, _find_only_cwl_commit(client, p)) for p in cwl_paths]

    cwl_paths = sorted(cwl_paths, key=lambda p: p[1].committed_date)

    for cwl_file, commit in cwl_paths:
        path = _migrate_cwl(client, cwl_file, commit)
        os.remove(cwl_file)

        client.repo.git.add(cwl_file, path)

        if client.repo.is_dirty():
            commit_msg = ('renku migrate: ' 'committing migrated workflow')

            committer = Actor('renku {0}'.format(__version__), version_url)

            client.repo.index.commit(
                commit_msg,
                committer=committer,
                skip_hooks=True,
            )


def _migrate_cwl(client, path, commit):
    """Migrate a cwl file."""
    workflow = parse_cwl_cached(str(path))

    if isinstance(workflow, CommandLineTool):
        _, path = _migrate_single_step(
            client, workflow, path, commit=commit, persist=True
        )
    else:
        _, path = _migrate_composite_step(
            client, workflow, path, commit=commit
        )

    return path


def _migrate_single_step(
    client, cmd_line_tool, path, commit=None, persist=False
):
    """Migrate a single step workflow."""
    if not commit:
        commit = client.find_previous_commit(path)

    run = Run(client=client, path=path, commit=commit)
    run.command = ' '.join(cmd_line_tool.baseCommand)
    run.successcodes = cmd_line_tool.successCodes

    inputs = list(cmd_line_tool.inputs)
    outputs = list(cmd_line_tool.outputs)

    if cmd_line_tool.stdin:
        name = cmd_line_tool.stdin.split('.')[1]

        if name.endswith(')'):
            name = name[:-1]

        matched_input = next(i for i in inputs if i.id == name)
        inputs.remove(matched_input)

        path = client.workflow_path / Path(matched_input.default['path'])
        stdin = path.resolve().relative_to(client.path)

        run.inputs.append(
            CommandInput(
                consumes=_entity_from_path(client, stdin, commit),
                mapped_to=MappedIOStream(stream_type='stdin')
            )
        )

    if cmd_line_tool.stdout:
        run.outputs.append(
            CommandOutput(
                produces=_entity_from_path(
                    client, cmd_line_tool.stdout, commit
                ),
                mapped_to=MappedIOStream(stream_type='stdout'),
                create_folder=False
            )
        )

        matched_output = next(o for o in outputs if o.id == 'output_stdout')

        if matched_output:
            outputs.remove(matched_output)

    if cmd_line_tool.stderr:
        run.outputs.append(
            CommandOutput(
                produces=_entity_from_path(
                    client, cmd_line_tool.stderr, commit
                ),
                mapped_to=MappedIOStream(stream_type='stderr'),
                create_folder=False
            )
        )

        matched_output = next(o for o in outputs if o.id == 'output_stderr')

        if matched_output:
            outputs.remove(matched_output)

    created_outputs = []
    workdir_requirements = [
        r for r in cmd_line_tool.requirements
        if isinstance(r, InitialWorkDirRequirement)
    ]

    for r in workdir_requirements:
        for l in r.listing:
            if l.entry == '$({"listing": [], "class": "Directory"})':
                created_outputs.append(l.entryname)

    for o in outputs:
        prefix = None
        position = None

        if o.outputBinding.glob.startswith('$(inputs.'):
            name = o.outputBinding.glob.split('.')[1]

            if name.endswith(')'):
                name = name[:-1]

            matched_input = next(i for i in inputs if i.id == name)
            inputs.remove(matched_input)

            if isinstance(matched_input.default, dict):
                path = client.workflow_path / Path(
                    matched_input.default['path']
                )
            else:
                path = Path(matched_input.default)

            path = Path(os.path.abspath(path)).relative_to(client.path)

            if matched_input.inputBinding:
                prefix = matched_input.inputBinding.prefix
                position = matched_input.inputBinding.position

                if prefix and matched_input.inputBinding.separate:
                    prefix += ' '
        else:
            path = Path(o.outputBinding.glob)

        create_folder = False

        check_path = path
        if not (client.path / path).is_dir():
            check_path = path.parent

        if check_path != '.' and str(check_path) in created_outputs:
            create_folder = True

        run.outputs.append(
            CommandOutput(
                position=position,
                prefix=prefix,
                produces=_entity_from_path(client, path, commit),
                create_folder=create_folder
            )
        )

    for i in inputs:
        prefix = None
        position = None

        if i.inputBinding:
            prefix = i.inputBinding.prefix
            position = i.inputBinding.position

            if prefix and i.inputBinding.separate:
                prefix += ' '

        if (
            isinstance(i.default, dict) and 'class' in i.default and
            i.default['class'] in ['File', 'Directory']
        ):
            path = client.workflow_path / Path(i.default['path'])
            path = Path(os.path.abspath(path)).relative_to(client.path)

            run.inputs.append(
                CommandInput(
                    position=position,
                    prefix=prefix,
                    consumes=_entity_from_path(client, path, commit)
                )
            )
        else:
            run.arguments.append(
                CommandArgument(
                    position=position, prefix=prefix, value=str(i.default)
                )
            )

    for a in cmd_line_tool.arguments:
        run.arguments.append(
            CommandArgument(position=a['position'], value=a['valueFrom'])
        )

    if not persist:
        return run, None

    step_name = '{0}_{1}.yaml'.format(
        uuid.uuid4().hex,
        secure_filename('_'.join(cmd_line_tool.baseCommand)),
    )

    path = (client.workflow_path / step_name).relative_to(client.path)

    with with_reference(path):
        run.path = path
        process_run = ProcessRun.from_run(run, client, path, commit=commit)
        process_run.invalidated = _invalidations_from_commit(client, commit)
        process_run.to_yaml()
        client.add_to_activity_index(process_run)
        return process_run, path


def _migrate_composite_step(client, workflow, path, commit=None):
    """Migrate a composite workflow."""
    if not commit:
        commit = client.find_previous_commit(path)
    run = Run(client=client, path=path, commit=commit)

    name = '{0}_migrated.yaml'.format(uuid.uuid4().hex)

    run.path = (client.workflow_path / name).relative_to(client.path)

    for step in workflow.steps:
        if isinstance(step.run, dict):
            continue
        else:
            path = client.workflow_path / step.run
            subrun = parse_cwl_cached(str(path))

        subprocess, _ = _migrate_single_step(
            client, subrun, path, commit=commit
        )
        subprocess.path = run.path
        run.add_subprocess(subprocess)

    with with_reference(run.path):
        wf = WorkflowRun.from_run(run, client, run.path, commit=commit)
        wf.to_yaml()
        client.add_to_activity_index(wf)

    return wf, run.path


def _entity_from_path(client, path, commit):
    """Gets the entity associated with a path."""
    client, commit, path = client.resolve_in_submodules(
        client.find_previous_commit(path, revision=commit.hexsha),
        path,
    )

    entity_cls = Entity
    if (client.path / path).is_dir():
        entity_cls = Collection

    if str(path).startswith(os.path.join(client.renku_home, client.DATASETS)):
        return client.load_dataset_from_path(path, commit=commit)
    else:
        return entity_cls(
            commit=commit,
            client=client,
            path=str(path),
        )


def _invalidations_from_commit(client, commit):
    """Gets invalidated files from a commit."""
    results = []
    collections = dict()
    for file_ in commit.diff(commit.parents or NULL_TREE):
        # only process deleted files (note they appear as ADDED)
        # in this backwards diff
        if file_.change_type != 'A':
            continue
        path_ = Path(file_.a_path)
        entity = _get_activity_entity(
            client, commit, path_, collections, deleted=True
        )

        results.append(entity)

    return results


def _get_activity_entity(client, commit, path, collections, deleted=False):
    """Gets the entity associated with this Activity and path."""
    client, commit, path = client.resolve_in_submodules(
        commit,
        path,
    )
    output_path = client.path / path
    parents = list(output_path.relative_to(client.path).parents)

    collection = None
    members = []
    for parent in reversed(parents[:-1]):
        if str(parent) in collections:
            collection = collections[str(parent)]
        else:
            collection = Collection(
                client=client,
                commit=commit,
                path=str(parent),
                members=[],
                parent=collection,
            )
            members.append(collection)
            collections[str(parent)] = collection

        members = collection.members

    entity_cls = Entity
    if (client.path / path).is_dir():
        entity_cls = Collection

    if str(path).startswith(
        os.path.join(client.renku_home, client.DATASETS)
    ) and not deleted:
        entity = client.load_dataset_from_path(path, commit=commit)
    else:
        entity = entity_cls(
            commit=commit,
            client=client,
            path=str(path),
            parent=collection,
        )

    if collection:
        collection.members.append(entity)

    return entity


_cwl_cache = {}


def parse_cwl_cached(path):
    """Parse cwl and remember the result for future execution."""
    if path in _cwl_cache:
        return _cwl_cache[path]

    cwl = parse_cwl(path)

    _cwl_cache[path] = cwl

    return cwl


def _find_only_cwl_commit(client, path, revision='HEAD'):
    """Find the most recent isolated commit of a cwl file.

    Commits with multiple cwl files are disregarded
    """
    file_commits = list(
        client.repo.iter_commits(revision, paths=path, full_history=True)
    )

    for commit in file_commits:
        cwl_files = [
            f for f in commit.stats.files.keys()
            if f.startswith(client.cwl_prefix) and path.endswith('.cwl')
        ]
        if len(cwl_files) == 1:
            return commit

    raise ValueError(
        "Couldn't find a previous commit for path {}".format(path)
    )
