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


class EventPinForm(forms.Form):
    pin = forms.RegexField(
        regex=r"^[0-9]{4}$",
        max_length=4,
        min_length=4,
        error_messages={"invalid": "Enter the four-digit event PIN."},
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
