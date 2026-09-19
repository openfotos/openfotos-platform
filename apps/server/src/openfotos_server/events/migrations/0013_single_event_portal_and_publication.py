import django.db.models.deletion
from django.db import migrations, models


def discard_non_portal_search_results(apps, schema_editor):
    FaceSearchResultSet = apps.get_model("events", "FaceSearchResultSet")
    FaceSearchResultSet.objects.filter(portal_capability__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0012_event_slug")]

    operations = [
        migrations.RunPython(discard_non_portal_search_results, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="facesearchresultset",
            name="face_search_has_one_capability",
        ),
        migrations.RemoveField(
            model_name="facesearchresultset",
            name="guest_capability",
        ),
        migrations.RemoveField(
            model_name="facesearchresultset",
            name="owner_capability",
        ),
        migrations.AlterField(
            model_name="facesearchresultset",
            name="portal_capability",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="face_search_results",
                to="events.portalcapability",
            ),
        ),
        migrations.DeleteModel(name="GuestCapability"),
        migrations.DeleteModel(name="OwnerCapability"),
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("photographer.created", "Photographer created"),
                    ("photographer.changed", "Photographer changed"),
                    ("membership.created", "Membership created"),
                    ("membership.changed", "Membership changed"),
                    ("event.created", "Event created"),
                    ("event.changed", "Event changed"),
                    ("event.state_changed", "Event state changed"),
                    ("portfolio.profile_changed", "Portfolio profile changed"),
                    ("portal_capability.issued", "Portfolio PIN issued"),
                    ("portal_capability.rotated", "Portfolio PIN rotated"),
                    ("portal_capability.pin_unlock", "Portfolio PIN unlock"),
                    ("event.media_purged", "Event media purged"),
                    ("photographer.login", "Photographer login"),
                    ("photographer.logout", "Photographer logout"),
                    ("share_access.reset", "Share access reset"),
                    ("face_search.completed", "Face search completed"),
                    ("face_search.rejected", "Face search rejected"),
                    ("original_download.issued", "Original download issued"),
                    ("desktop.login", "Desktop login"),
                    ("desktop.token_refresh", "Desktop token refresh"),
                    ("sub_event.created", "Sub-event created"),
                    ("sub_event.changed", "Sub-event changed"),
                    ("sub_event.archived", "Sub-event archived"),
                    ("sub_event.restored", "Sub-event restored"),
                    ("event_installation.registered", "Event installation registered"),
                    ("event_installation.revoked", "Event installation revoked"),
                    ("contribution.reserved", "Contribution reserved"),
                    ("contribution.reassigned", "Contribution reassigned"),
                    ("contribution.cancelled", "Contribution cancelled"),
                    ("asset_upload.verified", "Asset upload verified"),
                    ("asset.excluded", "Asset excluded"),
                    ("event.intake_closed", "Event intake closed"),
                    ("event.intake_reopened", "Event intake reopened"),
                    ("event.ingestion_finalized", "Event ingestion finalized"),
                    ("event.publication_snapshot", "Event publication snapshot committed"),
                    ("preview_policy.confirmed", "Preview policy confirmed"),
                    ("derivative_upload.verified", "Derivative upload verified"),
                    ("derivative.failed", "Derivative failed"),
                    ("face_analysis.completed", "Face analysis completed"),
                    ("face_analysis.failed", "Face analysis failed"),
                    ("face_analysis.conflict", "Face analysis conflicted"),
                    ("face_analysis.reset", "Face analysis reset"),
                    ("asset.gallery_excluded", "Asset excluded from gallery"),
                    ("asset.gallery_restored", "Asset restored to gallery"),
                ],
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="contributionbatch",
            name="state",
            field=models.CharField(
                choices=[
                    ("reserved", "Reserved"),
                    ("complete", "Complete"),
                    ("not_included", "Not Included"),
                    ("cancelled", "Cancelled"),
                ],
                default="reserved",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="ratelimitbucket",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("photographer_login", "Photographer login"),
                    ("portal_pin", "Portfolio PIN"),
                    ("face_search_client", "Face search by client"),
                    ("face_search_capability", "Face search by capability"),
                ],
                max_length=32,
            ),
        ),
    ]
