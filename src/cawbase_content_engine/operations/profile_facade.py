"""Public A02 methods of ``ScopedOperations`` (validation, permission checks, dispatch).

One business logic for Python and HTTP: the HTTP router only calls these methods.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from .. import validation as v
from ..contexts import ScopeContext
from ..errors import PermissionDeniedError, ResourceNotAccessibleError, ValidationFailedError
from ..models import (
    Category,
    DefaultProfile,
    Page,
    Profile,
    ProfileVersion,
    ProfileVersionSummary,
    RankingConfig,
    ResolvedProfile,
    SensitiveType,
    UpsertResult,
)
from ..permissions import ScopePermission
from ..transactions import HostTransaction
from . import elements as el
from . import profiles as pr
from . import versions as vs
from .common import page_size
from .profile_common import Actor, read_profile_row

if TYPE_CHECKING:
    from ..engine import ContentEngine

T = TypeVar("T")
UNSET: Any = pr.UNSET
_DEFAULT_PROFILE_STATUSES = ("active", "disabled")
_DEFAULT_ELEMENT_STATES = ("enabled", "disabled")


class ProfileOperations:
    _e: ContentEngine
    _ctx: ScopeContext
    _tx: HostTransaction | None

    # ---- plumbing --------------------------------------------------------------------------
    def _guard(self, scope_id: str, perm: ScopePermission) -> Actor:
        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:  # membership is validated; the id grants nothing
            raise ResourceNotAccessibleError()
        if perm not in self._ctx.permissions:
            raise PermissionDeniedError()
        return Actor(self._ctx.key_id, self._ctx.actor)

    @property
    def _t(self) -> Any:
        return self._e.tables

    @property
    def _lim(self) -> Any:
        return self._e.config.limits

    async def _read(self, fn: Callable[[AsyncConnection], Awaitable[T]]) -> T:
        return await self._e._run(fn, write=False, tx=self._tx)

    async def _write(self, fn: Callable[[AsyncConnection], Awaitable[T]]) -> T:
        return await self._e._run(fn, write=True, tx=self._tx)

    def _page(self, limit: int | None) -> int:
        return page_size(limit, self._lim.default_page_size, self._lim.max_page_size)

    @staticmethod
    def _filter(
        values: Collection[str] | str | None, field: str, allowed: tuple[str, ...], default: tuple[str, ...]
    ) -> tuple[str, ...]:
        if values is None:
            return default
        items = (values,) if isinstance(values, str) else tuple(values)
        if not items:
            return default
        return tuple(v.choice(i, field, allowed) for i in items)

    # ---- profiles --------------------------------------------------------------------------
    async def create_profile(
        self,
        scope_id: str,
        profile_id: str,
        display_name: str,
        *,
        description: str | None = None,
        language: str = "es",
        initial_catalog: str = "starter",
    ) -> UpsertResult[Profile]:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        name = v.text(display_name, "display_name", self._lim.max_display_name)
        desc = v.text(description, "description", self._lim.max_description, required=False)
        lang = v.choice(language, "language", v.LANGUAGES)
        cat = v.choice(initial_catalog, "initial_catalog", v.INITIAL_CATALOGS)
        assert name is not None
        return await self._write(
            lambda c: pr.create_profile(
                c,
                self._t,
                self._lim,
                actor,
                scope_id=scope_id,
                profile_id=pid,
                display_name=name,
                description=desc,
                language=lang,
                initial_catalog=cat,
            )
        )

    async def get_profile(self, scope_id: str, profile_id: str, *, include_deleted: bool = False) -> Profile:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        return await self._read(
            lambda c: pr.get_profile(
                c, self._t, scope_id=scope_id, profile_id=pid, include_deleted=bool(include_deleted)
            )
        )

    async def list_profiles(
        self,
        scope_id: str,
        *,
        status: Collection[str] | str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Profile]:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        statuses = self._filter(status, "status", v.PROFILE_STATUSES, _DEFAULT_PROFILE_STATUSES)
        n = self._page(limit)
        return await self._read(
            lambda c: pr.list_profiles(c, self._t, scope_id=scope_id, statuses=statuses, limit=n, cursor=cursor)
        )

    async def update_profile(
        self,
        scope_id: str,
        profile_id: str,
        *,
        expected_revision: int,
        display_name: Any = UNSET,
        description: Any = UNSET,
    ) -> Profile:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        rev = v.revision(expected_revision)
        changes: dict[str, Any] = {}
        if display_name is not UNSET:
            changes["display_name"] = v.text(display_name, "display_name", self._lim.max_display_name)
        if description is not UNSET:
            changes["description"] = v.text(description, "description", self._lim.max_description, required=False)
        if not changes:
            raise ValidationFailedError("No fields to update.")
        return await self._write(
            lambda c: pr.update_profile(
                c, self._t, actor, scope_id=scope_id, profile_id=pid, expected_revision=rev, changes=changes
            )
        )

    async def _profile_status(self, scope_id: str, profile_id: str, target: str) -> Profile:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        return await self._write(
            lambda c: pr.set_profile_status(c, self._t, actor, scope_id=scope_id, profile_id=pid, target=target)
        )

    async def disable_profile(self, scope_id: str, profile_id: str) -> Profile:
        return await self._profile_status(scope_id, profile_id, "disabled")

    async def enable_profile(self, scope_id: str, profile_id: str) -> Profile:
        return await self._profile_status(scope_id, profile_id, "active")

    async def delete_profile(self, scope_id: str, profile_id: str) -> Profile:
        return await self._profile_status(scope_id, profile_id, "deleted")

    # ---- ranking / resolution configuration ------------------------------------------------
    async def get_ranking_config(self, scope_id: str, profile_id: str) -> RankingConfig:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        return await self._read(lambda c: pr.get_ranking_config(c, self._t, scope_id=scope_id, profile_id=pid))

    async def update_ranking_config(
        self,
        scope_id: str,
        profile_id: str,
        *,
        expected_revision: int,
        mode: Any = UNSET,
        eligibility_threshold: Any = UNSET,
        tie_tolerance: Any = UNSET,
    ) -> RankingConfig:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        rev = v.revision(expected_revision)
        changes: dict[str, Any] = {}
        if mode is not UNSET:
            changes["mode"] = v.choice(mode, "mode", v.MODES)
        if eligibility_threshold is not UNSET:
            changes["eligibility_threshold"] = v.unit_float(eligibility_threshold, "eligibility_threshold")
        if tie_tolerance is not UNSET:
            changes["tie_tolerance"] = v.unit_float(tie_tolerance, "tie_tolerance")
        if not changes:
            raise ValidationFailedError("No fields to update.")
        return await self._write(
            lambda c: pr.update_ranking_config(
                c, self._t, actor, scope_id=scope_id, profile_id=pid, expected_revision=rev, changes=changes
            )
        )

    # ---- categories and sensitive types ----------------------------------------------------
    async def _profile_language(self, c: AsyncConnection, scope_id: str, profile_id: str) -> str:
        row = await read_profile_row(c, self._t, scope_id, profile_id)
        if row is None or row._mapping["status"] == "deleted":
            from ..errors import ResourceNotFoundError

            raise ResourceNotFoundError()
        return str(row._mapping["language"])

    async def _create_element(
        self,
        kind: el.Kind,
        scope_id: str,
        profile_id: str,
        element_id: str,
        name: Any,
        definition: Any,
        examples: Any,
        translations: Any,
        priority: Any,
    ) -> UpsertResult[Any]:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        eid = v.slug(element_id, kind.id_col)
        lim = self._lim
        nm = v.content_text(name, "name", lim.max_element_name)
        df = v.content_text(definition, "definition", lim.max_element_definition, multiline=True)
        ex = v.examples(examples, lim)
        prio = None
        if kind.has_priority and priority is not None:
            prio = v.int_in(priority, "priority", 1, lim.max_priority)

        async def fn(c: AsyncConnection) -> UpsertResult[Any]:
            tr = v.translations(translations, await self._profile_language(c, scope_id, pid), lim)
            v.element_bytes(nm, df, ex, tr, lim)
            values: dict[str, Any] = {"name": nm, "definition": df, "examples": ex, "translations": tr}
            if kind.has_priority:
                values["priority"] = prio
            return await el.create_element(
                c, self._t, lim, actor, kind, scope_id=scope_id, profile_id=pid, element_id=eid, values=values
            )

        return await self._write(fn)

    async def _update_element(
        self,
        kind: el.Kind,
        scope_id: str,
        profile_id: str,
        element_id: str,
        expected_revision: int,
        name: Any,
        definition: Any,
        examples: Any,
        translations: Any,
        priority: Any,
    ) -> Any:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        eid = v.slug(element_id, kind.id_col)
        rev = v.revision(expected_revision)
        lim = self._lim
        changes: dict[str, Any] = {}
        if name is not UNSET:
            changes["name"] = v.content_text(name, "name", lim.max_element_name)
        if definition is not UNSET:
            changes["definition"] = v.content_text(definition, "definition", lim.max_element_definition, multiline=True)
        if examples is not UNSET:
            changes["examples"] = v.examples(examples, lim)
        if priority is not UNSET:
            if not kind.has_priority:
                raise ValidationFailedError("Unknown field.", field="priority")
            changes["priority"] = None if priority is None else v.int_in(priority, "priority", 1, lim.max_priority)
        if not changes and translations is UNSET:
            raise ValidationFailedError("No fields to update.")

        async def fn(c: AsyncConnection) -> Any:
            ch = dict(changes)
            if translations is not UNSET:
                ch["translations"] = v.translations(translations, await self._profile_language(c, scope_id, pid), lim)
            # Size check on the resulting element (current values + changes).
            cur = await el.get_element(
                c, self._t, kind, scope_id=scope_id, profile_id=pid, element_id=eid, include_deleted=False
            )
            merged_tr = ch.get(
                "translations", {k: t_.model_dump() for k, t_ in cur.translations.items()} if cur.translations else None
            )
            merged_ex = ch.get(
                "examples", {"positive": list(cur.examples.positive), "negative": list(cur.examples.negative)}
            )
            v.element_bytes(ch.get("name", cur.name), ch.get("definition", cur.definition), merged_ex, merged_tr, lim)
            return await el.update_element(
                c,
                self._t,
                actor,
                kind,
                scope_id=scope_id,
                profile_id=pid,
                element_id=eid,
                expected_revision=rev,
                changes=ch,
            )

        return await self._write(fn)

    async def _element_state(self, kind: el.Kind, scope_id: str, profile_id: str, element_id: str, target: str) -> Any:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        eid = v.slug(element_id, kind.id_col)
        return await self._write(
            lambda c: el.set_element_state(
                c, self._t, actor, kind, scope_id=scope_id, profile_id=pid, element_id=eid, target=target
            )
        )

    async def _get_element(
        self, kind: el.Kind, scope_id: str, profile_id: str, element_id: str, include_deleted: bool
    ) -> Any:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        eid = v.slug(element_id, kind.id_col)
        return await self._read(
            lambda c: el.get_element(
                c,
                self._t,
                kind,
                scope_id=scope_id,
                profile_id=pid,
                element_id=eid,
                include_deleted=bool(include_deleted),
            )
        )

    async def _list_elements(
        self,
        kind: el.Kind,
        scope_id: str,
        profile_id: str,
        state: Collection[str] | str | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[Any]:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        states = self._filter(state, "state", v.ELEMENT_STATES, _DEFAULT_ELEMENT_STATES)
        n = self._page(limit)
        return await self._read(
            lambda c: el.list_elements(
                c, self._t, kind, scope_id=scope_id, profile_id=pid, states=states, limit=n, cursor=cursor
            )
        )

    # categories
    async def create_category(
        self,
        scope_id: str,
        profile_id: str,
        category_id: str,
        name: str,
        definition: str,
        *,
        examples: Any = None,
        translations: Any = None,
        priority: int | None = None,
    ) -> UpsertResult[Category]:
        return await self._create_element(
            el.CATEGORY, scope_id, profile_id, category_id, name, definition, examples, translations, priority
        )

    async def get_category(
        self, scope_id: str, profile_id: str, category_id: str, *, include_deleted: bool = False
    ) -> Category:
        return cast(Category, await self._get_element(el.CATEGORY, scope_id, profile_id, category_id, include_deleted))

    async def list_categories(
        self,
        scope_id: str,
        profile_id: str,
        *,
        state: Collection[str] | str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[Category]:
        return await self._list_elements(el.CATEGORY, scope_id, profile_id, state, limit, cursor)

    async def update_category(
        self,
        scope_id: str,
        profile_id: str,
        category_id: str,
        *,
        expected_revision: int,
        name: Any = UNSET,
        definition: Any = UNSET,
        examples: Any = UNSET,
        translations: Any = UNSET,
        priority: Any = UNSET,
    ) -> Category:
        return cast(
            Category,
            await self._update_element(
                el.CATEGORY,
                scope_id,
                profile_id,
                category_id,
                expected_revision,
                name,
                definition,
                examples,
                translations,
                priority,
            ),
        )

    async def disable_category(self, scope_id: str, profile_id: str, category_id: str) -> Category:
        return cast(Category, await self._element_state(el.CATEGORY, scope_id, profile_id, category_id, "disabled"))

    async def enable_category(self, scope_id: str, profile_id: str, category_id: str) -> Category:
        return cast(Category, await self._element_state(el.CATEGORY, scope_id, profile_id, category_id, "enabled"))

    async def delete_category(self, scope_id: str, profile_id: str, category_id: str) -> Category:
        return cast(Category, await self._element_state(el.CATEGORY, scope_id, profile_id, category_id, "deleted"))

    # sensitive types
    async def create_sensitive_type(
        self,
        scope_id: str,
        profile_id: str,
        type_id: str,
        name: str,
        definition: str,
        *,
        examples: Any = None,
        translations: Any = None,
    ) -> UpsertResult[SensitiveType]:
        return await self._create_element(
            el.SENSITIVE_TYPE, scope_id, profile_id, type_id, name, definition, examples, translations, None
        )

    async def get_sensitive_type(
        self, scope_id: str, profile_id: str, type_id: str, *, include_deleted: bool = False
    ) -> SensitiveType:
        return cast(
            SensitiveType, await self._get_element(el.SENSITIVE_TYPE, scope_id, profile_id, type_id, include_deleted)
        )

    async def list_sensitive_types(
        self,
        scope_id: str,
        profile_id: str,
        *,
        state: Collection[str] | str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[SensitiveType]:
        return await self._list_elements(el.SENSITIVE_TYPE, scope_id, profile_id, state, limit, cursor)

    async def update_sensitive_type(
        self,
        scope_id: str,
        profile_id: str,
        type_id: str,
        *,
        expected_revision: int,
        name: Any = UNSET,
        definition: Any = UNSET,
        examples: Any = UNSET,
        translations: Any = UNSET,
    ) -> SensitiveType:
        return cast(
            SensitiveType,
            await self._update_element(
                el.SENSITIVE_TYPE,
                scope_id,
                profile_id,
                type_id,
                expected_revision,
                name,
                definition,
                examples,
                translations,
                UNSET,
            ),
        )

    async def disable_sensitive_type(self, scope_id: str, profile_id: str, type_id: str) -> SensitiveType:
        return cast(
            SensitiveType, await self._element_state(el.SENSITIVE_TYPE, scope_id, profile_id, type_id, "disabled")
        )

    async def enable_sensitive_type(self, scope_id: str, profile_id: str, type_id: str) -> SensitiveType:
        return cast(
            SensitiveType, await self._element_state(el.SENSITIVE_TYPE, scope_id, profile_id, type_id, "enabled")
        )

    async def delete_sensitive_type(self, scope_id: str, profile_id: str, type_id: str) -> SensitiveType:
        return cast(
            SensitiveType, await self._element_state(el.SENSITIVE_TYPE, scope_id, profile_id, type_id, "deleted")
        )

    # ---- versions --------------------------------------------------------------------------
    async def publish_profile(
        self, scope_id: str, profile_id: str, *, expected_draft_revision: int
    ) -> UpsertResult[ProfileVersion]:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        rev = v.revision(expected_draft_revision, "expected_draft_revision")
        return await self._write(
            lambda c: vs.publish_profile(
                c, self._t, self._lim, actor, scope_id=scope_id, profile_id=pid, expected_draft_revision=rev
            )
        )

    async def get_profile_version(self, scope_id: str, profile_id: str, version_number: int) -> ProfileVersion:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        num = v.int_in(version_number, "version_number", 1)
        return await self._read(
            lambda c: vs.get_profile_version(c, self._t, scope_id=scope_id, profile_id=pid, version_number=num)
        )

    async def list_profile_versions(
        self, scope_id: str, profile_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Page[ProfileVersionSummary]:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = v.slug(profile_id, "profile_id")
        n = self._page(limit)
        return await self._read(
            lambda c: vs.list_profile_versions(c, self._t, scope_id=scope_id, profile_id=pid, limit=n, cursor=cursor)
        )

    # ---- default profile and resolution ----------------------------------------------------
    async def get_default_profile(self, scope_id: str) -> DefaultProfile:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        return await self._read(lambda c: pr.get_default_profile(c, self._t, scope_id=scope_id))

    async def set_default_profile(self, scope_id: str, profile_id: str, *, expected_revision: int) -> DefaultProfile:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        pid = v.slug(profile_id, "profile_id")
        rev = v.revision(expected_revision, low=0)
        return await self._write(
            lambda c: pr.set_default_profile(
                c, self._t, actor, scope_id=scope_id, profile_id=pid, expected_revision=rev
            )
        )

    async def clear_default_profile(self, scope_id: str, *, expected_revision: int) -> DefaultProfile:
        actor = self._guard(scope_id, ScopePermission.PROFILES_WRITE)
        rev = v.revision(expected_revision, low=0)
        return await self._write(
            lambda c: pr.clear_default_profile(c, self._t, actor, scope_id=scope_id, expected_revision=rev)
        )

    async def resolve_profile(
        self, scope_id: str, *, profile_id: str | None = None, version_number: int | None = None
    ) -> ResolvedProfile:
        self._guard(scope_id, ScopePermission.PROFILES_READ)
        pid = None if profile_id is None else v.slug(profile_id, "profile_id")
        num = None if version_number is None else v.int_in(version_number, "version_number", 1)
        return await self._read(
            lambda c: pr.resolve_profile(c, self._t, scope_id=scope_id, profile_id=pid, version_number=num)
        )
