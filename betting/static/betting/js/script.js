// betting/static/betting/js/script.js

// Function to handle placing a bet (example structure, adapt to your actual form submission)
document.addEventListener('DOMContentLoaded', function() {
    const placeBetForm = document.getElementById('place-bet-form'); // Assuming you have a form with this ID
    const userWalletBalanceSpan = document.getElementById('user-wallet-balance'); // Get the span element

    if (placeBetForm) {
        placeBetForm.addEventListener('submit', function(event) {
            event.preventDefault(); // Prevent default form submission

            // Collect your bet data (example, adapt to your form fields)
            const selections = [];
            document.querySelectorAll('.fixture-selection:checked').forEach(checkbox => {
                selections.push({
                    fixture_id: checkbox.dataset.fixtureId,
                    bet_type: checkbox.value // e.g., 'D', 'H', 'A'
                });
            });
            const stake = parseFloat(document.getElementById('stake-input').value); // Assuming stake input ID
            const isNap = document.getElementById('is-nap-checkbox').checked; // Assuming a checkbox
            const permutationCount = parseInt(document.getElementById('permutation-count-input').value); // Assuming permutation count input

            const betData = {
                selections: selections,
                stake: stake,
                is_nap: isNap,
                permutation_count: permutationCount
            };

            fetch('/place-bet/', { // Your place-bet URL
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCookie('csrftoken') // Function to get CSRF token
                    },
                    body: JSON.stringify(betData)
                })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        alert(data.message); // Show success message
                        // Update the wallet balance in the navbar
                        if (userWalletBalanceSpan && data.new_balance) {
                            userWalletBalanceSpan.textContent = '₦' + parseFloat(data.new_balance).toFixed(2);
                        }
                        // Optionally, clear selections or reset form
                        placeBetForm.reset();
                        // Or redirect, refresh part of the page, etc.
                    } else {
                        alert('Error: ' + data.message); // Show error message
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    alert('An error occurred while placing your bet.');
                });
        });
    }

    // Helper function to get CSRF token (if you don't have one)
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                // Does this cookie string begin with the name we want?
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    // You might also want a periodic refresh for balance for other transactions (winnings etc.)
    // This part is optional but good for truly "real-time" feel if not using WebSockets
    // function refreshWalletBalance() {
    //     if (userWalletBalanceSpan) {
    //         fetch('/api/get-wallet-balance/') // Create this endpoint if needed
    //             .then(response => response.json())
    //             .then(data => {
    //                 if (data.status === 'success' && data.balance) {
    //                     userWalletBalanceSpan.textContent = '₦' + parseFloat(data.balance).toFixed(2);
    //                 }
    //             })
    //             .catch(error => console.error('Error refreshing wallet balance:', error));
    //     }
    // }

    // Call refreshWalletBalance every 30 seconds (adjust interval as needed)
    // setInterval(refreshWalletBalance, 30000);
});