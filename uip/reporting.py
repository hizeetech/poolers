import csv
import datetime
from django.http import HttpResponse
from django.utils import timezone
from django.db import models
from django.db.models import Sum
from betting.models import BetTicket, Transaction, ActivityLog
from .models import DailyMetricSnapshot

class ReportingService:
    @staticmethod
    def generate_csv_response(filename, header, rows):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
        
        writer = csv.writer(response)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            
        return response

    @staticmethod
    def export_financial_report(start_date, end_date):
        """
        Exports daily financial metrics (Turnover, GGR, NGR) for a date range.
        """
        snapshots = DailyMetricSnapshot.objects.filter(
            date__range=(start_date, end_date)
        ).order_by('date')
        
        header = ['Date', 'Total Stake', 'Winnings Paid', 'GGR', 'Net Profit', 'Tickets Sold', 'Active Users']
        rows = []
        for s in snapshots:
            rows.append([
                s.date,
                s.total_stake_volume,
                s.total_winnings_paid,
                s.gross_gaming_revenue,
                s.net_profit,
                s.total_tickets_sold,
                s.active_users_count
            ])
            
        filename = f"financial_report_{start_date}_{end_date}"
        return ReportingService.generate_csv_response(filename, header, rows)

    @staticmethod
    def export_agent_performance_report(start_date, end_date):
        """
        Exports agent performance based on ticket sales.
        """
        # This is a bit more complex aggregation, let's keep it simple for now
        # We need to aggregate BetTicket by user where user_type is agent
        tickets = BetTicket.objects.filter(
            placed_at__date__range=(start_date, end_date)
        ).exclude(user__user_type='player') # Agents/Cashiers
        
        # Aggregate by user
        from django.db.models import Count, F
        
        agent_stats = tickets.values(
            username=F('user__email'), 
            role=F('user__user_type')
        ).annotate(
            total_sales=Sum('stake_amount'),
            ticket_count=Count('id'),
            total_payouts=Sum('max_winning', filter=models.Q(status='won'))
        ).order_by('-total_sales')
        
        header = ['Agent Email', 'Role', 'Total Sales', 'Ticket Count', 'Total Payouts']
        rows = []
        for stat in agent_stats:
            rows.append([
                stat['username'],
                stat['role'],
                stat['total_sales'],
                stat['ticket_count'],
                stat['total_payouts'] or 0
            ])
            
        filename = f"agent_performance_{start_date}_{end_date}"
        return ReportingService.generate_csv_response(filename, header, rows)

    @staticmethod
    def export_audit_log_report(start_date, end_date):
        """
        Exports audit/activity logs.
        """
        logs = ActivityLog.objects.filter(
            timestamp__date__range=(start_date, end_date)
        ).order_by('-timestamp')
        
        header = ['Timestamp', 'User', 'Action Type', 'Action', 'IP Address', 'Affected Object']
        rows = []
        for log in logs:
            rows.append([
                log.timestamp,
                log.user.email if log.user else 'System',
                log.action_type,
                log.action,
                log.ip_address,
                log.affected_object
            ])
            
        filename = f"audit_log_{start_date}_{end_date}"
        return ReportingService.generate_csv_response(filename, header, rows)
