import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0007_assetobject_content_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="photographer",
            name="contact_phone",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="photographer",
            name="instagram_url",
            field=models.URLField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="photographer",
            name="logo_height",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="photographer",
            name="logo_object_key",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="photographer",
            name="logo_sha256",
            field=models.CharField(
                blank=True,
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[0-9a-f]{64}$", "Enter a lowercase SHA-256 digest."
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="photographer",
            name="logo_width",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="cover_height",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="cover_object_key",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="event",
            name="cover_sha256",
            field=models.CharField(
                blank=True,
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[0-9a-f]{64}$", "Enter a lowercase SHA-256 digest."
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="cover_width",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="first_published_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="media_purged_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="purge_after",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name="ConsentAttestation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("notice_version", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="openfotos_consent_attestations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="consent_attestation",
                        to="events.event",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PortalCapability",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("pin_hash", models.CharField(editable=False, max_length=256)),
                ("access_version", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="portal_capability",
                        to="events.event",
                    ),
                ),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="facesearchresultset",
            name="face_search_has_one_capability",
        ),
        migrations.AddField(
            model_name="facesearchresultset",
            name="portal_capability",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="face_search_results",
                to="events.portalcapability",
            ),
        ),
        migrations.AddConstraint(
            model_name="facesearchresultset",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ("guest_capability__isnull", True),
                        ("owner_capability__isnull", False),
                        ("portal_capability__isnull", True),
                    )
                    | models.Q(
                        ("guest_capability__isnull", False),
                        ("owner_capability__isnull", True),
                        ("portal_capability__isnull", True),
                    )
                    | models.Q(
                        ("guest_capability__isnull", True),
                        ("owner_capability__isnull", True),
                        ("portal_capability__isnull", False),
                    )
                ),
                name="face_search_has_one_capability",
            ),
        ),
    ]
