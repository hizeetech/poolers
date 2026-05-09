from django.contrib import admin
from django.utils.html import format_html
from django.urls import path
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils.crypto import get_random_string
from .models import PendingAgentRegistration
from django.contrib.auth import get_user_model
from betting.models import Wallet, Transaction, State
from betting.admin import betting_admin_site
import random
import re
from betting.services.usernames import (
    generate_agent_username,
    generate_cashier_usernames,
    generate_cashier_email,
)

User = get_user_model()

@admin.register(PendingAgentRegistration)
class PendingAgentRegistrationAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'user_type', 'registered_by', 'status', 'created_at', 'actions_buttons')
    list_filter = ('status', 'user_type', 'created_at')
    search_fields = ('full_name', 'email', 'registered_by__email')
    readonly_fields = ('password',) 
    
    def get_queryset(self, request):
        return super().get_queryset(request)

    def actions_buttons(self, obj):
        if obj.status == 'PENDING':
            return format_html(
                '<a class="button" href="{}">Approve</a>&nbsp;'
                '<a class="button" href="{}" style="background-color:red;">Reject</a>',
                f"approve/{obj.id}/",
                f"reject/{obj.id}/",
            )
        if obj.status == 'APPROVED':
            return format_html(
                '<span style="margin-right:6px;">APPROVED</span>'
                '<a class="button" href="{}">Resend Credentials</a>',
                f"resend/{obj.id}/",
            )
        return obj.status
    actions_buttons.short_description = 'Actions'
    actions_buttons.allow_tags = True

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('approve/<int:pk>/', self.admin_site.admin_view(self.approve_agent), name='approve_agent'),
            path('reject/<int:pk>/', self.admin_site.admin_view(self.reject_agent), name='reject_agent'),
            path('resend/<int:pk>/', self.admin_site.admin_view(self.resend_credentials), name='resend_credentials'),
        ]
        return custom_urls + urls

    def resend_credentials(self, request, pk):
        pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
        if pending_reg.status != 'APPROVED':
            messages.warning(request, "This registration is not approved.")
            return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

        user = User.objects.filter(email__iexact=pending_reg.email).first()
        if not user:
            messages.error(request, "Approved registration has no matching user account.")
            return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

        raw_password = get_random_string(12)
        cashier_accounts = []
        try:
            with transaction.atomic():
                user.set_password(raw_password)
                user.save(update_fields=["password"])

                if user.user_type == 'agent':
                    cashiers = User.objects.filter(user_type='cashier', agent=user).order_by('id')
                    for cashier in cashiers:
                        cashier.set_password(raw_password)
                        cashier.save(update_fields=["password"])
                        cashier_accounts.append(cashier)

            login_url = request.build_absolute_uri('/login/')
            html_message = render_to_string('pending_registration/email/agent_approved.html', {
                'user': user,
                'cashier_accounts': cashier_accounts,
                'login_url': login_url,
                'password': raw_password,
            })
            send_mail(
                subject='Pool Betting Agent Registration Approved',
                message=strip_tags(html_message),
                from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False
            )
            messages.success(request, f"Credentials resent to {user.email}.")
        except Exception as e:
            messages.error(request, f"Failed to resend credentials: {e}")

        return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

    def approve_agent(self, request, pk):
        pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
        if pending_reg.status != 'PENDING':
             messages.warning(request, "This registration is not pending.")
             return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

        try:
            with transaction.atomic():
                raw_password = get_random_string(12)
                name_parts = (pending_reg.full_name or "").split()
                first_name = name_parts[0] if name_parts else ""
                last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                other_name = ""

                state_obj = None
                if pending_reg.state:
                    state_obj = State.objects.filter(state_name__iexact=pending_reg.state).first()
                    if not state_obj:
                        state_obj = State.objects.filter(abbreviation__iexact=pending_reg.state).first()

                username = None
                roots = []
                base_root = ""
                if state_obj:
                    try:
                        username, roots, base_root = generate_agent_username(
                            User,
                            state_obj.abbreviation,
                            first_name,
                            last_name,
                            other_name,
                        )
                    except Exception:
                        username = None

                if not username:
                    local = (pending_reg.email or "").split("@")[0]
                    local = re.sub(r"[^A-Za-z0-9]", "", local)[:20] or "Agent"
                    candidate = local[:1].upper() + local[1:].lower()
                    suffix = 1
                    while User.objects.filter(username__iexact=candidate).exists():
                        candidate = f"{local}{suffix}"
                        suffix += 1
                    username = candidate

                user = User.objects.create_user(
                    email=pending_reg.email,
                    password=raw_password,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    other_name=other_name,
                    phone_number=pending_reg.phone,
                    state=state_obj,
                    shop_address=pending_reg.state,
                    user_type=pending_reg.user_type,
                    master_agent=pending_reg.master_agent,
                    super_agent=pending_reg.super_agent,
                    is_active=True
                )

                # 2. Create Wallet (if not created by signals)
                if not Wallet.objects.filter(user=user).exists():
                    Wallet.objects.create(user=user, balance=0)

                cashier_accounts = []
                if user.user_type == 'agent':
                    while True:
                        prefix = str(random.randint(1000, 9999))
                        if not User.objects.filter(cashier_prefix=prefix).exists():
                            break
                    user.cashier_prefix = prefix
                    user.save(update_fields=["cashier_prefix"])

                    cashier1_username, cashier2_username, _cashier_root = generate_cashier_usernames(
                        User,
                        preferred_root=user.username,
                        roots=roots,
                        base_root=base_root,
                    )

                    cashier_specs = [
                        ("C1", cashier1_username, f"{prefix}-01"),
                        ("C2", cashier2_username, f"{prefix}-02"),
                    ]

                    for code, cashier_username, cashier_prefix in cashier_specs:
                        cashier_email = generate_cashier_email(user.email, code)
                        if User.objects.filter(email__iexact=cashier_email).exists():
                            continue
                        cashier = User.objects.create_user(
                            email=cashier_email,
                            password=raw_password,
                            username=cashier_username,
                            first_name=user.first_name,
                            last_name=user.last_name,
                            other_name=user.other_name,
                            state=user.state,
                            user_type='cashier',
                            agent=user,
                            master_agent=user.master_agent,
                            super_agent=user.super_agent,
                            cashier_prefix=cashier_prefix,
                            is_active=True
                        )
                        cashier_accounts.append(cashier)
                        if not Wallet.objects.filter(user=cashier).exists():
                            Wallet.objects.create(user=cashier, balance=0)

                # 5. Update Status
                pending_reg.status = 'APPROVED'
                pending_reg.reviewed_at = timezone.now()
                pending_reg.save()
                
                # 6. Send Approval Email
                login_url = request.build_absolute_uri('/login/')
                html_message = render_to_string('pending_registration/email/agent_approved.html', {
                    'user': user,
                    'cashier_accounts': cashier_accounts,
                    'login_url': login_url,
                    'password': raw_password,
                })
                try:
                    send_mail(
                        subject='Pool Betting Agent Registration Approved',
                        message=strip_tags(html_message),
                        from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                        recipient_list=[user.email],
                        html_message=html_message,
                        fail_silently=False
                    )
                except Exception as e:
                    messages.warning(request, f"Agent created but approval email failed to send: {e}")
                
            messages.success(request, f"Agent {user.email} approved and created successfully.")
            
        except Exception as e:
            messages.error(request, f"Error approving agent: {e}")
            
        return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

    def reject_agent(self, request, pk):
        pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
        
        if request.method == 'POST':
            reason = request.POST.get('reason')
            pending_reg.status = 'REJECTED'
            pending_reg.admin_notes = reason
            pending_reg.reviewed_at = timezone.now()
            pending_reg.save()
            
            # Send Rejection Email
            html_message = render_to_string('pending_registration/email/agent_rejected.html', {
                'pending_reg': pending_reg,
                'reason': reason
            })
            try:
                send_mail(
                    subject='Pool Betting Agent Registration Rejected',
                    message=strip_tags(html_message),
                    from_email=settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
                    recipient_list=[pending_reg.email],
                    html_message=html_message,
                    fail_silently=False
                )
            except Exception as e:
                messages.warning(request, f"Rejection email failed to send: {e}")
            
            messages.info(request, "Agent registration rejected.")
            return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')
            
        return render(request, 'pending_registration/admin/reject_reason.html', {'pending_reg': pending_reg})

# Register with the CUSTOM admin site
betting_admin_site.register(PendingAgentRegistration, PendingAgentRegistrationAdmin)
