(function($) {
    $(document).ready(function() {
        console.log("Weekly Commission Admin JS Loaded");
        var agentField = $('#id_agent');
        var periodField = $('#id_period');

        function fetchCommissionData() {
            var agentId = agentField.val();
            var periodId = periodField.val();
            
            console.log("Fetching commission data for Agent:", agentId, "Period:", periodId);

            if (agentId && periodId) {
                // Show loading state
                $('.field-total_stake .readonly').text('Loading...');
                $('.field-total_winnings .readonly').text('Loading...');
                $('.field-ggr .readonly').text('Loading...');
                $('.field-commission_ggr_amount .readonly').text('Loading...');
                $('.field-commission_hybrid_amount .readonly').text('Loading...');
                $('.field-commission_total_amount .readonly').text('Loading...');

                $.ajax({
                    url: '/commission/api/calculate-commission/',
                    data: {
                        'agent_id': agentId,
                        'period_id': periodId
                    },
                    success: function(data) {
                        console.log("Commission data received:", data);
                        // Update readonly fields
                        $('.field-total_stake .readonly').text(data.total_stake);
                        $('.field-total_winnings .readonly').text(data.total_winnings);
                        $('.field-ggr .readonly').text(data.ggr);
                        $('.field-commission_ggr_amount .readonly').text(data.commission_ggr_amount);
                        $('.field-commission_hybrid_amount .readonly').text(data.commission_hybrid_amount);
                        $('.field-commission_total_amount .readonly').text(data.commission_total_amount);
                    },
                    error: function(xhr) {
                        console.error('Error fetching commission data:', xhr.responseText);
                        var errorMsg = 'Error';
                        if (xhr.responseJSON && xhr.responseJSON.error) {
                            errorMsg = xhr.responseJSON.error;
                        }
                        
                        $('.field-total_stake .readonly').text(errorMsg);
                        $('.field-total_winnings .readonly').text('-');
                        $('.field-ggr .readonly').text('-');
                        $('.field-commission_ggr_amount .readonly').text('-');
                        $('.field-commission_hybrid_amount .readonly').text('-');
                        $('.field-commission_total_amount .readonly').text('-');
                    }
                });
            }
        }

        agentField.change(fetchCommissionData);
        periodField.change(fetchCommissionData);
        
        // Support for Select2 (Django Autocomplete Light or standard admin Select2)
        // Select2 triggers 'select2:select' on the original element
        agentField.on('select2:select', fetchCommissionData);
        periodField.on('select2:select', fetchCommissionData);

        // Initial check in case of browser autofill or edit mode
        fetchCommissionData();
    });
})(django.jQuery);
