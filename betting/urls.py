# betting/urls.py

from django.urls import path
from . import views

app_name = 'betting' # <--- ADDED THIS LINE

urlpatterns = [
    # General Authentication
    path('', views.frontpage, name='frontpage'),
    path('register/', views.register_user, name='register'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='user_logout'),

    # Fixtures & Betting
    path('fixtures/', views.fixtures_view, name='fixtures'),
    path('fixtures/partial/', views.fixtures_list_partial, name='fixtures_list_partial'),
    path('fixtures/partial/<int:period_id>/', views.fixtures_list_partial, name='fixtures_list_partial_with_period'),
    path('fixtures/<int:period_id>/', views.fixtures_view, name='fixtures_with_period'),
    path('place-bet/', views.place_bet, name='place_bet'),
    path('check-ticket/', views.check_ticket_status, name='check_ticket_status'),
    path('agent-void-ticket/<str:ticket_id>/', views.agent_void_ticket, name='agent_void_ticket'),

    # Wallet & Payments
    path('wallet/', views.wallet_view, name='wallet'),
    path('deposit/initiate/', views.initiate_deposit, name='initiate_deposit'),
    path('deposit/verify/', views.verify_deposit, name='verify_deposit'),
    path('withdraw/', views.withdraw_funds, name='withdraw_funds'),
    path('wallet-transfer/', views.wallet_transfer, name='wallet_transfer'),
    path('credit-request/submit/', views.submit_credit_request, name='submit_credit_request'),
    path('credit-request/manage/', views.manage_credit_requests, name='manage_credit_requests'),
    path('credit-request/approve/<int:request_id>/', views.approve_credit_request, name='approve_credit_request'),
    path('loan/settle/<int:loan_id>/', views.settle_loan, name='settle_loan'),

    # User Profile & Dashboard
    path('dashboard/', views.user_dashboard, name='user_dashboard'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password, name='change_password'),

    # Agent/Super Agent/Master Agent specific URLs
    path('agent/dashboard/', views.agent_dashboard, name='agent_dashboard'),
    path('agent/cashier-list/', views.agent_cashier_list, name='agent_cashier_list'),
    path('agent/cashier/create/', views.agent_create_cashier, name='agent_create_cashier'),
    path('agent/cashier/edit/<int:cashier_id>/', views.agent_edit_cashier, name='agent_edit_cashier'),
    path('agent/cashier/delete/<int:cashier_id>/', views.agent_delete_cashier, name='agent_delete_cashier'),
    path('agent/cashier/credit/<int:cashier_id>/', views.agent_credit_cashier, name='agent_credit_cashier'),
    path('master-agent/dashboard/', views.master_agent_dashboard, name='master_agent_dashboard'),
    path('super-agent/dashboard/', views.super_agent_dashboard, name='super_agent_dashboard'),
    path('downline/users/', views.downline_users, name='downline_users'),
    path('downline/bets/', views.downline_bets, name='downline_bets'),

    # Reporting URLs
    path('reports/wallet/', views.agent_wallet_report, name='agent_wallet_report'),
    path('reports/sales-winnings/', views.agent_sales_winnings_report, name='agent_sales_winnings_report'),
    path('reports/commission/', views.agent_commission_report, name='agent_commission_report'),
    path('reports/admin-commission-financial/', views.admin_commission_financial_report, name='admin_commission_financial_report'),

    # Admin Dashboard URLs (if these are handled via views.py, not betting_admin_site)
    path('admin_dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('manage_users/', views.manage_users, name='manage_users'),
    path('edit_user/<int:user_id>/', views.edit_user, name='edit_user'),
    path('delete_user/<int:user_id>/', views.delete_user, name='delete_user'), # Assuming you have a delete_user view
    path('manage_fixtures/', views.manage_fixtures, name='manage_fixtures'),
    path('add_fixture/', views.add_fixture, name='add_fixture'),
    path('edit_fixture/<int:fixture_id>/', views.edit_fixture, name='edit_fixture'),
    path('delete_fixture/<int:fixture_id>/', views.delete_fixture, name='delete_fixture'),
    path('declare_result/<int:fixture_id>/', views.declare_result, name='declare_result'),
    path('withdrawals/', views.withdraw_request_list, name='withdraw_request_list'),
    path('withdrawals/<int:withdrawal_id>/action/', views.approve_reject_withdrawal, name='approve_reject_withdrawal'),
    path('manage_betting_periods/', views.manage_betting_periods, name='manage_betting_periods'),
    path('add_betting_period/', views.add_betting_period, name='add_betting_period'),
    path('edit_betting_period/<int:period_id>/', views.edit_betting_period, name='edit_betting_period'),
    path('delete_betting_period/<int:period_id>/', views.delete_betting_period, name='delete_betting_period'),
    path('manage_agent_payouts/', views.manage_agent_payouts, name='manage_agent_payouts'),
    path('mark_payout_settled/<int:payout_id>/', views.mark_payout_settled, name='mark_payout_settled'),
    path('admin_ticket_report/', views.admin_ticket_report, name='admin_ticket_report'),
    path('admin_ticket_details/<uuid:ticket_id>/', views.admin_ticket_details, name='admin_ticket_details'),
    path('admin_void_ticket_single/<uuid:ticket_id>/', views.admin_void_ticket_single, name='admin_void_ticket_single'),
    path('admin_settle_won_ticket_single/<uuid:ticket_id>/', views.admin_settle_won_ticket_single, name='admin_settle_won_ticket_single'),

    # Account User URLs
    path('account-user/dashboard/', views.account_user_dashboard, name='account_user_dashboard'),
    path('super-admin/fund-account-user/', views.super_admin_fund_account_user, name='super_admin_fund_account_user'),

    # API endpoints
    path('api/betting-periods/', views.api_betting_periods, name='api_betting_periods'),
    path('api/fixtures/', views.api_fixtures, name='api_fixtures'),
    path('api/place-bet/', views.api_place_bet, name='api_place_bet'),
    path('api/check-ticket-status/', views.api_check_ticket_status, name='api_check_ticket_status'),
    path('api/user-wallet/', views.api_user_wallet, name='api_user_wallet'),
    path('api/deposit/initiate/', views.api_initiate_deposit, name='api_initiate_deposit'),
    path('api/deposit/verify/', views.api_verify_deposit, name='api_verify_deposit'),
    path('api/withdraw-funds/', views.api_withdraw_funds, name='api_withdraw_funds'),
    path('api/wallet-transfer/', views.api_wallet_transfer, name='api_wallet_transfer'),
    path('api/user-profile/', views.api_user_profile, name='api_user_profile'),
    path('api/change-password/', views.api_change_password, name='api_change_password'),
    path('api/user-transactions/', views.api_user_transactions, name='api_user_transactions'),
    path('api/agent-commissions/', views.api_agent_commissions, name='api_agent_commissions'),
    path('api/agent-users/', views.api_agent_users, name='api_agent_users'),
    path('api/cashier-transactions/', views.api_cashier_transactions, name='api_cashier_transactions'),

    path('api/bet-tickets/', views.api_bet_tickets, name='api_bet_tickets'),
    path('api/void-ticket/', views.api_void_ticket, name='api_void_ticket'),
    path('api/manage-users/', views.api_manage_users, name='api_manage_users'),
    path('api/system-settings/', views.api_system_settings, name='api_system_settings'),
    
    # Impersonation URLs
    path('impersonate/<int:user_id>/', views.impersonate_user, name='impersonate_user'),
    path('impersonate/stop/', views.stop_impersonation, name='stop_impersonation'),
]
