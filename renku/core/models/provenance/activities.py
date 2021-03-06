# -*- coding: utf-8 -*-
#
# Copyright 2018-2020 - Swiss Data Science Center (SDSC)
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
"""Represent a Git commit."""

import os
import urllib
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path, posixpath

import attr
from git import NULL_TREE

from renku.core.models import jsonld
from renku.core.models.cwl.annotation import Annotation
from renku.core.models.entities import Collection, CommitMixin, Entity
from renku.core.models.refs import LinkReference
from renku.core.models.workflow.run import Run

from .agents import Person, renku_agent
from .qualified import Association, Generation, Usage


def _nodes(output, parent=None):
    """Yield nodes from entities."""
    # NOTE refactor so all outputs behave the same
    entity = getattr(output, 'entity', output)

    if isinstance(entity, Collection):
        for member in entity.members:
            if parent is not None:
                member = attr.evolve(member, parent=parent)

            if entity.client:
                _set_entity_client_commit(member, entity.client, None)
            if isinstance(output, Generation):
                child = Generation(
                    activity=output.activity,
                    entity=member,
                    role=entity.role if hasattr(entity, 'role') else None
                )
            elif isinstance(output, Usage):
                child = Usage(
                    activity=output.activity,
                    entity=member,
                    role=entity.role if hasattr(entity, 'role') else None
                )
            else:
                child = member
            yield from _nodes(child)

    yield output


def _set_entity_client_commit(entity, client, commit):
    """Set the client and commit of an entity."""
    if client and not entity.client:
        entity.client = client

    if not entity.commit:
        revision = 'UNCOMMITTED'
        if entity._label:
            revision = entity._label.rsplit('@')[1]
        if revision == 'UNCOMMITTED':
            commit = commit
        elif client:
            commit = client.repo.commit(revision)
        entity.commit = commit


@jsonld.s(
    type='prov:Activity',
    context={
        'prov': 'http://www.w3.org/ns/prov#',
        'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
        'schema': 'http://schema.org/'
    },
    cmp=False,
)
class Activity(CommitMixin):
    """Represent an activity in the repository."""

    _id = jsonld.ib(default=None, context='@id', kw_only=True)
    _message = jsonld.ib(context='rdfs:comment', kw_only=True)
    _was_informed_by = jsonld.ib(
        context='prov:wasInformedBy',
        kw_only=True,
    )

    part_of = attr.ib(default=None, kw_only=True)

    _collections = attr.ib(
        default=attr.Factory(OrderedDict), init=False, kw_only=True
    )
    generated = jsonld.container.list(
        Generation, context={
            '@reverse': 'prov:activity',
        }, kw_only=True
    )

    invalidated = jsonld.container.list(
        Entity, context={
            '@reverse': 'prov:wasInvalidatedBy',
        }, kw_only=True
    )

    influenced = jsonld.ib(
        context='prov:influenced',
        kw_only=True,
    )

    started_at_time = jsonld.ib(
        context={
            '@id': 'prov:startedAtTime',
            '@type': 'http://www.w3.org/2001/XMLSchema#dateTime',
        },
        kw_only=True,
    )

    ended_at_time = jsonld.ib(
        context={
            '@id': 'prov:endedAtTime',
            '@type': 'http://www.w3.org/2001/XMLSchema#dateTime',
        },
        kw_only=True,
    )

    agent = jsonld.ib(
        context='prov:wasAssociatedWith',
        kw_only=True,
        default=renku_agent,
        type='renku.core.models.provenance.agents.SoftwareAgent'
    )
    person_agent = jsonld.ib(
        context='prov:wasAssociatedWith',
        kw_only=True,
        type='renku.core.models.provenance.agents.Person'
    )

    def default_generated(self):
        """Create default ``generated``."""
        generated = []

        for path in self.get_output_paths():
            entity = self._get_activity_entity(path)

            generated.append(
                Generation(activity=self, entity=entity, role=None)
            )
        return generated

    def get_output_paths(self):
        """Gets all output paths generated by this run."""
        index = set()

        commit = self.commit

        if not self.commit:
            if not self.client:
                return index
            commit = self.client.repo.head.commit

        for file_ in commit.diff(commit.parents or NULL_TREE):
            # ignore deleted files (note they appear as ADDED)
            # in this backwards diff
            if file_.change_type == 'A':
                continue
            path_ = Path(file_.a_path)

            is_dataset = self.client.DATASETS in str(path_)
            not_refs = LinkReference.REFS not in str(path_)
            does_not_exists = not path_.exists()

            if all([is_dataset, not_refs, does_not_exists]):
                uid = uuid.UUID(path_.parent.name)
                path_ = (
                    Path(self.client.renku_home) / self.client.DATASETS /
                    str(uid) / self.client.METADATA
                )

            index.add(str(path_))

        return index

    def _get_activity_entity(self, path, deleted=False):
        """Gets the entity associated with this Activity and path."""
        client, commit, path = self.client.resolve_in_submodules(
            self.commit,
            path,
        )
        output_path = client.path / path
        parents = list(output_path.relative_to(client.path).parents)

        collection = None
        members = []
        for parent in reversed(parents[:-1]):
            if str(parent) in self._collections:
                collection = self._collections[str(parent)]
            else:
                collection = Collection(
                    client=client,
                    commit=commit,
                    path=str(parent),
                    members=[],
                    parent=collection,
                )
                members.append(collection)
                self._collections[str(parent)] = collection

            members = collection.members

        entity_cls = Entity
        if (self.client.path / path).is_dir():
            entity_cls = Collection

        # TODO: use a factory method to generate the entity
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

    def default_invalidated(self):
        """Entities invalidated by this Action."""
        results = []
        for path in self.removed_paths:
            entity = self._get_activity_entity(path, deleted=True)

            results.append(entity)
        return results

    @influenced.default
    def default_influenced(self):
        """Calculate default values."""
        return list(self._collections.values())

    @property
    def parents(self):
        """Return parent commits."""
        if self.commit:
            return list(self.commit.parents)

    @property
    def removed_paths(self):
        """Return all paths removed in the commit."""
        index = set()
        if not self.commit:
            return index

        for file_ in self.commit.diff(self.commit.parents or NULL_TREE):
            # only process deleted files (note they appear as ADDED)
            # in this backwards diff
            if file_.change_type != 'A':
                continue
            path_ = Path(file_.a_path)

            index.add(str(path_))

        return index

    @property
    def paths(self):
        """Return all paths in the commit."""
        index = set()

        for file_ in self.commit.diff(self.commit.parents or NULL_TREE):
            # ignore deleted files (note they appear as ADDED)
            # in this backwards diff
            if file_.change_type == 'A':
                continue
            path_ = Path(file_.a_path)

            is_dataset = self.client.DATASETS in str(path_)
            not_refs = LinkReference.REFS not in str(path_)
            does_not_exists = not (
                path_.exists() or
                (path_.is_symlink() and os.path.lexists(path_))
            )

            if all([is_dataset, not_refs, does_not_exists]):
                uid = uuid.UUID(path_.parent.name)
                path_ = (
                    Path(self.client.renku_home) / self.client.DATASETS /
                    str(uid) / self.client.METADATA
                )

            index.add(str(path_))

        return index

    @classmethod
    def generate_id(cls, commitsha):
        """Calculate action ID."""
        host = 'localhost'
        if hasattr(cls, 'client'):
            host = cls.client.remote.get('host') or host
        host = os.environ.get('RENKU_DOMAIN') or 'localhost'

        # always set the id by the identifier
        return urllib.parse.urljoin(
            'https://{host}'.format(host=host),
            posixpath.join(
                '/activities', 'commit/{commit}'.format(commit=commitsha)
            )
        )

    def default_id(self):
        """Configure calculated ID."""
        if self.commit:
            return self.generate_id(self.commit.hexsha)
        return self.generate_id('UNCOMMITED')

    @_message.default
    def default_message(self):
        """Generate a default message."""
        if self.commit:
            return self.commit.message

    @_was_informed_by.default
    def default_was_informed_by(self):
        """List parent actions."""
        if self.commit:
            return [{
                '@id': self.generate_id(parent),
            } for parent in self.commit.parents]

    @started_at_time.default
    def default_started_at_time(self):
        """Configure calculated properties."""
        if self.commit:
            return self.commit.authored_datetime.isoformat()

    @ended_at_time.default
    def default_ended_at_time(self):
        """Configure calculated properties."""
        if self.commit:
            return self.commit.committed_datetime.isoformat()

    @person_agent.default
    def default_person_agent(self):
        """Set person agent to be the author of the commit."""
        if self.commit:
            return Person.from_commit(self.commit)
        return None

    @property
    def nodes(self):
        """Return topologically sorted nodes."""
        collections = OrderedDict()

        def _parents(node):
            if node.parent:
                yield from _parents(node.parent)
                yield node.parent

        for output in self.generated:
            for parent in _parents(output.entity):
                collections[parent.path] = parent

            yield from _nodes(output)

        for removed in self.invalidated:
            for parent in _parents(removed):
                collections[parent.path] = parent

            yield from _nodes(removed)

        yield from reversed(collections.values())

    def __attrs_post_init__(self):
        """Sets ``generated`` and ``invalidated`` default values if needed."""
        super().__attrs_post_init__()
        if not self._id:
            self._id = self.default_id()
        if not self.generated:
            self.generated = self.default_generated()

        for g in self.generated:
            _set_entity_client_commit(g.entity, self.client, self.commit)

        if not self.invalidated:
            self.invalidated = self.default_invalidated()

        if self.generated:
            for g in self.generated:
                g._activity = weakref.ref(self)


@jsonld.s(
    type='wfprov:ProcessRun',
    context={
        'wfprov': 'http://purl.org/wf4ever/wfprov#',
        'oa': 'http://www.w3.org/ns/oa#',
    },
    cmp=False,
)
class ProcessRun(Activity):
    """A process run is a particular execution of a Process description."""

    __association_cls__ = Run

    generated = jsonld.container.list(
        Generation,
        context={
            '@reverse': 'prov:activity',
        },
        kw_only=True,
        default=None
    )

    association = jsonld.ib(
        context='prov:qualifiedAssociation',
        default=None,
        kw_only=True,
        type=Association
    )

    annotations = jsonld.container.list(
        context={
            '@reverse': 'oa:hasTarget',
        }, kw_only=True, type=Annotation
    )

    qualified_usage = jsonld.container.list(
        Usage, context='prov:qualifiedUsage', kw_only=True, default=None
    )

    def __attrs_post_init__(self):
        """Calculate properties."""
        super().__attrs_post_init__()

        if not self.commit and self.client:
            self.commit = self.client.find_previous_commit(self.path)

        if not self.annotations:
            self.annotations = self.plugin_annotations()

        if self.association:
            self.association.plan._activity = weakref.ref(self)
            plan = self.association.plan
            if not plan.commit:
                if self.client:
                    plan.client = self.client
                if self.commit:
                    plan.commit = self.commit

                if plan.inputs:
                    for i in plan.inputs:
                        _set_entity_client_commit(
                            i.consumes, self.client, self.commit
                        )
                if plan.outputs:
                    for o in plan.outputs:
                        _set_entity_client_commit(
                            o.produces, self.client, self.commit
                        )

        if self.qualified_usage and self.client and self.commit:
            usages = []
            revision = '{0}'.format(self.commit)
            for usage in self.qualified_usage:
                if not usage.commit and '@UNCOMMITTED' in usage._label:
                    usages.append(
                        Usage.from_revision(
                            client=self.client,
                            path=usage.path,
                            role=usage.role,
                            revision=revision,
                            id=usage._id,
                        )
                    )
                else:
                    if not usage.client:
                        usage.entity.set_client(self.client)
                    if not usage.commit:
                        revision = usage._label.rsplit('@')[1]
                        usage.entity.commit = self.client.repo.commit(revision)

                    usages.append(usage)
            self.qualified_usage = usages

    def default_generated(self):
        """Create default ``generated``."""
        generated = []

        if not self.association or not self.association.plan:
            return generated

        for output in self.association.plan.outputs:
            entity = Entity.from_revision(
                self.client,
                output.produces.path,
                revision=self.commit,
                parent=output.produces.parent
            )

            generation = Generation(
                activity=self, role=output.sanitized_id, entity=entity
            )
            generated.append(generation)
        return generated

    def add_annotations(self, annotations):
        """Adds annotations from an external tool."""
        self.annotations.extend(annotations)

    def plugin_annotations(self):
        """Adds ``Annotation``s from plugins to a ``ProcessRun``."""
        from renku.core.plugins.pluginmanager import get_plugin_manager
        pm = get_plugin_manager()

        results = pm.hook.process_run_annotations(run=self)
        return [a for r in results for a in r]

    @classmethod
    def from_run(
        cls,
        run,
        client,
        path,
        commit=None,
        is_subprocess=False,
        update_commits=False
    ):
        """Convert a ``Run`` to a ``ProcessRun``."""
        from .agents import SoftwareAgent

        if not commit:
            commit = client.repo.head.commit

        usages = []

        id_ = ProcessRun.generate_id(commit)

        if is_subprocess:
            id_ = '{}/steps/step_{}'.format(id_, run.process_order)

        for input_ in run.inputs:
            usage_id = id_ + '/inputs/' + input_.sanitized_id
            revision = commit
            input_path = input_.consumes.path
            entity = input_.consumes
            if update_commits:
                revision = client.find_previous_commit(
                    input_path, revision=commit.hexsha
                )
                entity = Entity.from_revision(client, input_path, revision)

            dependency = Usage(
                entity=entity, role=input_.sanitized_id, id=usage_id
            )

            usages.append(dependency)

        agent = SoftwareAgent.from_commit(commit)
        association = Association(
            agent=agent, id=id_ + '/association', plan=run
        )

        process_run = cls(
            id=id_,
            qualified_usage=usages,
            association=association,
            client=client,
            commit=commit,
            path=path
        )

        generated = []

        for output in run.outputs:
            entity = Entity.from_revision(
                client,
                output.produces.path,
                revision=commit,
                parent=output.produces.parent
            )

            generation = Generation(
                activity=process_run, role=output.sanitized_id, entity=entity
            )
            generated.append(generation)

        process_run.generated = generated

        process_run.plugin_annotations()
        return process_run

    @property
    def parents(self):
        """Return parent commits."""
        return [
            member.commit for usage in self.qualified_usage
            for member in usage.entity.entities
        ] + super().parents

    @property
    def nodes(self):
        """Return topologically sorted nodes."""
        # Outputs go first
        yield from super().nodes

        # Activity itself
        yield self.association.plan


@jsonld.s(
    type='wfprov:WorkflowRun',
    context={
        'wfprov': 'http://purl.org/wf4ever/wfprov#',
    },
    cmp=False,
)
class WorkflowRun(ProcessRun):
    """A workflow run typically contains several subprocesses."""

    __association_cls__ = Run

    _processes = jsonld.container.list(
        ProcessRun,
        context={
            '@reverse': 'wfprov:wasPartOfWorkflowRun',
        },
        kw_only=True,
        default=attr.Factory(list)
    )

    @property
    def subprocesses(self):
        """Subprocesses of this ``WorkflowRun``."""
        return {p.association.plan.process_order: p for p in self._processes}

    @classmethod
    def from_run(cls, run, client, path, commit=None, update_commits=False):
        """Convert a ``Run`` to a ``WorkflowRun``."""
        from .agents import SoftwareAgent

        if not commit:
            commit = client.repo.head.commit

        processes = []
        generated = []

        for s in run.subprocesses:
            proc_run = ProcessRun.from_run(
                s, client, path, commit, True, update_commits
            )
            processes.append(proc_run)
            generated.extend(proc_run.generated)

        usages = []

        id_ = cls.generate_id(commit)

        for input in run.inputs:
            usage_id = id_ + '/inputs/' + input.sanitized_id
            dependency = Usage.from_revision(
                client=client,
                path=input.consumes.path,
                role=input.sanitized_id,
                revision=commit,
                id=usage_id,
            )
            usages.append(dependency)

        agent = SoftwareAgent.from_commit(commit)
        association = Association(
            agent=agent, id=id_ + '/association', plan=run
        )

        all_generated = []

        # fix generations in folders
        for generation in generated:
            all_generated.append(generation)
            entity = generation.entity

            if not isinstance(entity, Collection) or not entity.commit:
                continue

            for e in entity.entities:
                if e.commit is not entity.commit:
                    continue

                all_generated.append(
                    Generation(
                        activity=generation.activity, entity=e, role=None
                    )
                )

        wf_run = WorkflowRun(
            id=id_,
            processes=processes,
            generated=all_generated,
            qualified_usage=usages,
            association=association,
            client=client,
            commit=commit,
            path=path
        )
        return wf_run

    @property
    def nodes(self):
        """Yield all graph nodes."""
        for subprocess in reversed(self._processes):
            if subprocess.path is None:
                # skip nodes connecting directory to file
                continue

            for n in subprocess.nodes:
                # if self.client and not n.commit and isinstance(n, Entity):
                #     _set_entity_client_commit(n, self.client, self.commit)
                # n._activity = weakref.ref(subprocess)
                yield n
            yield subprocess.association.plan

    def __attrs_post_init__(self):
        """Attrs post initializations."""
        if not self._id:
            self._id = self.default_id()

        if self.client:
            for s in self._processes:
                s.client = self.client
                s.commit = self.commit
                s.__attrs_post_init__()
                s.part_of = self

        super().__attrs_post_init__()
