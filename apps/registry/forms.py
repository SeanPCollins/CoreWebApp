from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordResetForm

class SignUpForm(forms.Form):
    first_name = forms.CharField(max_length=30)
    last_name = forms.CharField(max_length=30)
    numerical_suffix = forms.CharField(required=False)
    username = forms.CharField(max_length=150, required=False)
    email = forms.EmailField()
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)
    reason = forms.CharField(widget=forms.Textarea)

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip().lower()
        suffix = self.cleaned_data.get('numerical_suffix', '')

        if suffix and not suffix.isdigit():
            raise forms.ValidationError("The username suffix must contain numbers only.")

        first_name = self.cleaned_data.get('first_name', '').strip()
        last_name = self.cleaned_data.get('last_name', '').strip()
        if not first_name or not last_name:
            return ''
        base_username = f"{first_name[0]}{last_name}".lower()
        username = username or base_username

        if suffix:
            username = f"{username}{suffix}"

        if username != base_username:
            selected_suffix = username[len(base_username):] if username.startswith(base_username) else ''
            if not selected_suffix.isdigit():
                raise forms.ValidationError(
                    "Your username is generated from your name. Choose the suggested username or a numbered option."
                )

        if User.objects.filter(username__iexact=username).exists():
            if not self.data.get('username', '').strip():
                base_username = username
                suffix_number = 2
                while User.objects.filter(
                    username__iexact=f"{base_username}{suffix_number}"
                ).exists():
                    suffix_number += 1
                username = f"{base_username}{suffix_number}"
            else:
                raise forms.ValidationError(
                    "This username is already taken. Please choose one of the numbered suggestions."
                )

        return username

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords do not match.")
        return cleaned_data

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email


class UsernamePasswordResetForm(PasswordResetForm):
    email = forms.CharField(
        label="Username",
        max_length=150,
        widget=forms.TextInput(attrs={
            'autocomplete': 'username',
            'autofocus': True,
        }),
    )

    def clean_email(self):
        return self.cleaned_data['email'].strip()

    def get_users(self, username):
        user = User.objects.filter(username=username).first()
        if user is None:
            users = list(User.objects.filter(username__iexact=username).order_by('pk')[:2])
            if len(users) != 1:
                return
            user = users[0]
        if user.is_active and user.has_usable_password() and user.email:
            yield user

class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Your full name'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'your.email@hc-sc.gc.ca'})
    )
    subject = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Brief summary of the issue'})
    )
    message = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'class': 'form-textarea', 'rows': 5, 'placeholder': 'Describe your issue or data inquiry in detail...'})
    )
