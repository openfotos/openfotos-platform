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
