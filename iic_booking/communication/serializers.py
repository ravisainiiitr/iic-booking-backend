"""Serializers for communication models."""

from rest_framework import serializers
from .models import Notice


class NoticeSerializer(serializers.ModelSerializer):
    """Serializer for Notice model."""
    
    created_by_name = serializers.CharField(source='created_by.name', read_only=True)
    notice_type_display = serializers.CharField(source='get_notice_type_display', read_only=True)

    class Meta:
        model = Notice
        fields = [
            'notice_id',
            'title',
            'description',
            'content',
            'notice_type',
            'notice_type_display',
            'is_active',
            'priority',
            'created_by',
            'created_by_name',
            'expiry_date',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['notice_id', 'created_at', 'updated_at', 'created_by_name', 'notice_type_display']
