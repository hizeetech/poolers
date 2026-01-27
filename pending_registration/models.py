from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class PendingAgentRegistration(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )
    
    USER_TYPE_CHOICES = (
        ('agent', 'Agent'),
        ('super_agent', 'Super Agent'),
        # Add others if needed, but feature scope implies mostly Agents
    )

    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    password = models.CharField(max_length=128) # Stores hashed password
    state = models.CharField(max_length=100, blank=True, null=True)
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='agent')
    
    # Hierarchy
    master_agent = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='pending_master_registrations')
    super_agent = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='pending_super_registrations')
    
    registered_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_registrations')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    
    admin_notes = models.TextField(blank=True, null=True) # For rejection reason

    def __str__(self):
        return f"{self.full_name} ({self.email}) - {self.status}"
