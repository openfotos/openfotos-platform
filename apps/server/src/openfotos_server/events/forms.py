"""Narrow browser forms for account and event access."""

from django import forms

from openfotos_contracts import OriginalDownloadPolicy


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
        regex=r"^[0-9]{6}$",
        max_length=6,
        min_length=6,
        error_messages={"invalid": "Enter the six-digit event PIN."},
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]{6}",
            }
        ),
    )


class DownloadPolicyForm(forms.Form):
    policy = forms.ChoiceField(
        choices=tuple(
            (policy.value, policy.value.replace("-", " ").title())
            for policy in OriginalDownloadPolicy
        )
    )


class GalleryExclusionForm(forms.Form):
    reason = forms.CharField(max_length=240, strip=True)
