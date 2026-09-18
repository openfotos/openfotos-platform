"""Narrow browser forms for account and event access."""

from django import forms


class PhotographerLoginForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )
    password = forms.CharField(
        max_length=256,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class SharePinForm(forms.Form):
    pin = forms.RegexField(
        regex=r"^[0-9]{4}$",
        max_length=4,
        min_length=4,
        error_messages={"invalid": "Enter the four-digit PIN."},
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]{4}",
            }
        ),
    )


class SubEventForm(forms.Form):
    name = forms.CharField(max_length=120, strip=True)
    position = forms.IntegerField(min_value=1, max_value=32_767)


class BatchReassignmentForm(forms.Form):
    sub_event_id = forms.UUIDField()


class GalleryExclusionForm(forms.Form):
    reason = forms.CharField(max_length=240, strip=True)


class GuestCapabilityForm(forms.Form):
    label = forms.CharField(max_length=80, strip=True, required=False)
    sub_event_id = forms.ChoiceField(label="Gallery scope")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sub_event_id"].choices = [
            ("", "All Photos"),
            *[(str(value.id), value.name) for value in event.sub_events.filter(is_archived=False)],
        ]


class FaceSearchForm(forms.Form):
    reference_photo = forms.FileField(
        label="Reference photo",
        widget=forms.FileInput(
            attrs={
                "accept": "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif",
            }
        ),
    )
    consent = forms.BooleanField(
        label=(
            "I have permission to use this person's photo and understand that the image, "
            "face crop, and search vector are discarded after processing."
        )
    )
