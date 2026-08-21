"""UserSerializer must not 500 when ChannelIIdentityProfile table is absent."""

from unittest.mock import PropertyMock, patch

from django.test import SimpleTestCase

from iic_booking.users.serializers.user_serializer import UserSerializer


class UserSerializerPreMigrationChannelITests(SimpleTestCase):
    def test_gender_from_channel_i_false_when_relation_raises(self):
        user = type("U", (), {"pk": 1, "gender": ""})()

        class Boom:
            def __get__(self, obj, objtype=None):
                raise Exception('relation "users_channeliidentityprofile" does not exist')

        # Simulate reverse OneToOne access raising ProgrammingError-like failure
        with patch.object(
            type(user),
            "channel_i_identity",
            Boom(),
            create=True,
        ):
            ser = UserSerializer()
            self.assertFalse(ser.get_gender_from_channel_i(user))
