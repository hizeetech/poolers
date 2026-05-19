(function($) {
    $(document).ready(function() {
        // Find the header for the "Pay Now?" column
        var header = $('.column-is_marked_for_payment .text');
        if (header.length) {
            // Create a checkbox to put in the header
            var selectAll = $('<input type="checkbox" id="select-all-pay-now" style="margin-left: 10px;" title="Select all for payment">');
            header.append(selectAll);

            selectAll.on('change', function() {
                var checked = $(this).prop('checked');
                // Find all checkboxes in the same column
                $('.field-is_marked_for_payment input[type="checkbox"]').each(function() {
                    $(this).prop('checked', checked);
                });
            });
        }
    });
})(django.jQuery);
