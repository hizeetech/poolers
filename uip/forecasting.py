from django.utils import timezone
from django.db.models import Sum, Avg, Count
from betting.models import BetTicket
from .models import DailyMetricSnapshot
from datetime import timedelta
import math

class ForecastingService:
    @staticmethod
    def predict_turnover():
        """
        Predicts turnover for the next 7 days using a weighted moving average
        of the last 30 days of data from DailyMetricSnapshot.
        """
        today = timezone.now().date()
        start_date = today - timedelta(days=30)
        
        # Get historical data
        history = DailyMetricSnapshot.objects.filter(
            date__gte=start_date
        ).order_by('date').values('date', 'total_stake_volume')
        
        data_points = list(history)
        
        if not data_points:
            return {
                'prediction_type': 'Insufficient Data',
                'next_7_days': []
            }

        # Simple Linear Regression (y = mx + c)
        n = len(data_points)
        x = list(range(n))
        y = [float(d['total_stake_volume']) for d in data_points]
        
        if n > 1:
            x_mean = sum(x) / n
            y_mean = sum(y) / n
            
            numerator = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
            denominator = sum((xi - x_mean) ** 2 for xi in x)
            
            slope = numerator / denominator if denominator != 0 else 0
            intercept = y_mean - (slope * x_mean)
        else:
            slope = 0
            intercept = y[0]

        predictions = []
        current_trend = "Stable"
        if slope > 0.5: current_trend = "Growing"
        elif slope < -0.5: current_trend = "Declining"

        for i in range(1, 8):
            future_x = n - 1 + i
            predicted_value = (slope * future_x) + intercept
            predictions.append({
                'date': today + timedelta(days=i),
                'predicted_turnover': max(0, round(predicted_value, 2))
            })
            
        return {
            'prediction_type': 'Linear Regression',
            'trend': current_trend,
            'slope': round(slope, 2),
            'next_7_days': predictions
        }

    @staticmethod
    def identify_peak_periods():
        """
        Identifies peak betting hours based on recent BetTicket data.
        """
        # Analyze last 7 days of tickets
        start_date = timezone.now() - timedelta(days=7)
        
        # Django doesn't have a simple "ExtractHour" that works identically across all DBs without setup,
        # but ExtractHour is standard.
        from django.db.models.functions import ExtractHour
        
        peak_hours = BetTicket.objects.filter(
            placed_at__gte=start_date
        ).annotate(
            hour=ExtractHour('placed_at')
        ).values('hour').annotate(
            count=Count('id'),
            volume=Sum('stake_amount')
        ).order_by('-volume')[:5]
        
        return list(peak_hours)
