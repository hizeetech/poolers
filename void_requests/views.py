from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from betting.models import BetTicket

from .services import create_void_request


@login_required
@require_POST
def create_ticket_void_request(request):
    ticket_id = (request.POST.get("ticket_id") or "").strip().upper()
    reason = (request.POST.get("reason") or "").strip()
    if request.user.user_type != "cashier":
        return JsonResponse({"success": False, "message": "Only cashiers can request void."}, status=403)
    if not ticket_id:
        return JsonResponse({"success": False, "message": "Missing ticket_id."}, status=400)

    ticket = get_object_or_404(BetTicket, ticket_id=ticket_id)
    try:
        create_void_request(ticket=ticket, cashier=request.user, reason=reason)
    except PermissionError as e:
        return JsonResponse({"success": False, "message": str(e)}, status=403)
    except ValueError as e:
        return JsonResponse({"success": False, "message": str(e)}, status=400)
    except Exception:
        return JsonResponse({"success": False, "message": "Unable to create void request."}, status=500)

    return JsonResponse({"success": True, "message": "Ticket has been sent for void approval."})

