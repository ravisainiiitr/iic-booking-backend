from storages.backends.s3boto3 import S3Boto3Storage


class MediaStorage(S3Boto3Storage):
    """
    S3-backed storage for media files.

    We keep media under the `media/` prefix in the bucket, so the DB stores paths
    like `equipment_images/...` and the storage maps them to `media/equipment_images/...`.
    """

    location = "media"
    file_overwrite = False


class EquipmentImageStorage(MediaStorage):
    """
    S3-backed storage for equipment photos.

    Uses the same bucket and `media/` prefix as default media storage so images
    survive production deploys (unlike local filesystem storage under MEDIA_ROOT).
    """

    location = "media"
    file_overwrite = False
