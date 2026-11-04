from django.contrib import admin

from .models import LearningModule, LearningSetting, LogbookEntry, ModuleProgress, QuizAttempt, QuizQuestion


@admin.register(LearningModule)
class LearningModuleAdmin(admin.ModelAdmin):
    list_display = ("title", "equipment", "is_required", "pass_mark_pct", "is_published")
    list_filter = ("is_published", "is_required")
    raw_id_fields = ("equipment", "created_by")


for model in (LearningSetting, QuizQuestion, ModuleProgress, QuizAttempt, LogbookEntry):
    admin.site.register(model)
