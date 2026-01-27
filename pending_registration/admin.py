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
from .models import PendingAgentRegistration
from django.contrib.auth import get_user_model
from betting.models import Wallet, Transaction
from betting.admin import betting_admin_site
import random

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
        return obj.status
    actions_buttons.short_description = 'Actions'
    actions_buttons.allow_tags = True

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('approve/<int:pk>/', self.admin_site.admin_view(self.approve_agent), name='approve_agent'),
            path('reject/<int:pk>/', self.admin_site.admin_view(self.reject_agent), name='reject_agent'),
        ]
        return custom_urls + urls

    def approve_agent(self, request, pk):
        pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
        if pending_reg.status != 'PENDING':
             messages.warning(request, "This registration is not pending.")
             return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')

        try:
            with transaction.atomic():
                # 1. Create User
                user = User.objects.create_user(
                    email=pending_reg.email,
                    password=None, # Will set hash directly
                    first_name=pending_reg.full_name.split()[0],
                    last_name=" ".join(pending_reg.full_name.split()[1:]) if " " in pending_reg.full_name else "",
                    phone_number=pending_reg.phone,
                    shop_address=pending_reg.state, 
                    user_type=pending_reg.user_type,
                    master_agent=pending_reg.master_agent,
                    super_agent=pending_reg.super_agent,
                    is_active=True
                )
                user.password = pending_reg.password # Already hashed
                user.save()

                # 2. Create Wallet (if not created by signals)
                if not Wallet.objects.filter(user=user).exists():
                    Wallet.objects.create(user=user, balance=0)

                # 3. Generate Cashier Prefix for the Agent
                # Ensure unique 4-digit prefix
                while True:
                    prefix = str(random.randint(1000, 9999))
                    if not User.objects.filter(cashier_prefix=prefix).exists():
                        break
                
                user.cashier_prefix = prefix
                user.save()

                # 4. Create 2 Cashier Accounts
                cashier_emails = []
                for i in range(1, 3):
                    cashier_email = f"{prefix}-CSH-{i:02d}@cashier.com"
                    cashier_emails.append(cashier_email)
                    
                    # Check if cashier already exists (highly unlikely with unique prefix, but good for safety)
                    if not User.objects.filter(email=cashier_email).exists():
                        cashier = User.objects.create_user(
                            email=cashier_email,
                            password=None,
                            first_name=f"Cashier {i}",
                            last_name=f"for {pending_reg.full_name}",
                            user_type='cashier',
                            agent=user, 
                            cashier_prefix=f"{prefix}-{i:02d}",  # Set the cashier_prefix for the cashier account as well
                            is_active=True
                        )
                        cashier.password = pending_reg.password
                        cashier.save()
                        
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
                    'cashier_emails': cashier_emails,
                    'login_url': login_url
                })
                send_mail(
                    subject='Pool Betting Agent Registration Approved',
                    message=strip_tags(html_message),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    html_message=html_message,
                    fail_silently=True
                )
                
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
            send_mail(
                subject='Pool Betting Agent Registration Rejected',
                message=strip_tags(html_message),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[pending_reg.email],
                html_message=html_message,
                fail_silently=True
            )
            
            messages.info(request, "Agent registration rejected.")
            return redirect(f'{self.admin_site.name}:pending_registration_pendingagentregistration_changelist')
            
        return render(request, 'pending_registration/admin/reject_reason.html', {'pending_reg': pending_reg})

# Register with the CUSTOM admin site
betting_admin_site.register(PendingAgentRegistration, PendingAgentRegistrationAdmin)
