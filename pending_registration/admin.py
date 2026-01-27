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
             return redirect('admin:pending_registration_pendingagentregistration_changelist')

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

                # 3. Create Cashier Account
                email_prefix = pending_reg.email.split('@')[0]
                cashier_email = f"cashier1_{pending_reg.email}" 
                
                cashier = User.objects.create_user(
                    email=cashier_email,
                    password=None,
                    first_name="Cashier1",
                    last_name=f"for {pending_reg.full_name}",
                    user_type='cashier',
                    agent=user, 
                    is_active=True
                )
                cashier.password = pending_reg.password
                cashier.save()
                
                if not Wallet.objects.filter(user=cashier).exists():
                    Wallet.objects.create(user=cashier, balance=0)

                # 4. Update Status
                pending_reg.status = 'APPROVED'
                pending_reg.reviewed_at = timezone.now()
                pending_reg.save()
                
                # 5. Send Approval Email
                try:
                    login_url = request.build_absolute_uri('/login/')
                    html_message = render_to_string('pending_registration/email/agent_approved.html', {
                        'user': user,
                        'cashier_email': cashier_email,
                        'login_url': login_url
                    })
                    plain_message = strip_tags(html_message)
                    
                    send_mail(
                        subject='Pool Betting Agent Registration Approved',
                        message=plain_message,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[user.email],
                        html_message=html_message,
                        fail_silently=True
                    )
                except Exception as e:
                    messages.warning(request, f"Agent created but failed to send email: {e}")

                messages.success(request, f"Agent {user.email} created successfully with cashier {cashier.email}. Email notification sent.")
                
        except Exception as e:
            messages.error(request, f"Error approving agent: {e}")
            
        return redirect('admin:pending_registration_pendingagentregistration_changelist')

    def reject_agent(self, request, pk):
        pending_reg = get_object_or_404(PendingAgentRegistration, pk=pk)
        
        if request.method == 'POST':
            reason = request.POST.get('reason')
            pending_reg.status = 'REJECTED'
            pending_reg.admin_notes = reason
            pending_reg.reviewed_at = timezone.now()
            pending_reg.save()
            
            # Send Rejection Email
            try:
                html_message = render_to_string('pending_registration/email/agent_rejected.html', {
                    'full_name': pending_reg.full_name,
                    'reason': reason,
                })
                plain_message = strip_tags(html_message)
                
                send_mail(
                    subject='Pool Betting Agent Registration Rejected',
                    message=plain_message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[pending_reg.email],
                    html_message=html_message,
                    fail_silently=True
                )
            except Exception as e:
                messages.warning(request, f"Rejection recorded but failed to send email: {e}")

            messages.info(request, "Registration rejected and email sent.")
            return redirect('admin:pending_registration_pendingagentregistration_changelist')
        
        # Simple template for rejection reason
        context = dict(
           self.admin_site.each_context(request),
           object=pending_reg,
        )
        return render(request, 'pending_registration/admin/reject_reason.html', context)
