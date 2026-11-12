"""Shared model bases for IITR Connect."""

from __future__ import annotations

from django.db import models


class ImmutableRecordError(Exception):
    """Raised when code tries to change or delete an append-only Connect record."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only.")

    def delete(self):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only.")


class AppendOnlyModel(models.Model):
    """Audit rows: written once, never updated or deleted through the ORM."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(f"{type(self).__name__} rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} rows are append-only.")


class Visibility(models.TextChoices):
    VISIBLE = "visible", "Visible"
    HIDDEN = "hidden", "Hidden by a moderator"
    REMOVED = "removed", "Removed by a moderator"
    DELETED = "deleted", "Deleted by the author"


VISIBLE_STATES = (Visibility.VISIBLE,)
