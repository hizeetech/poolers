document.addEventListener('DOMContentLoaded', function() {
    // Function to initialize the toggle functionality
    function initOddsToggle() {
        // Strategy: Find ALL active checkboxes first
        const allActiveCheckboxes = document.querySelectorAll('input[type="checkbox"][name^="active_"]');
        
        if (allActiveCheckboxes.length === 0) {
            return; 
        }

        // We use the first checkbox to determine where to insert the toggle
        const firstActiveCheckbox = allActiveCheckboxes[0];

        // Check if toggle already exists to prevent duplicates
        if (document.getElementById('toggle-all-odds')) {
            return;
        }

        // --- ROBUST ROW FINDING ---
        // Try to find the container of the first row
        // Standard Django Admin uses .form-row
        // Jazzmin/Bootstrap uses .form-group or .row
        const firstRow = firstActiveCheckbox.closest('.form-row') || 
                         firstActiveCheckbox.closest('.form-group') || 
                         firstActiveCheckbox.closest('.row') ||
                         firstActiveCheckbox.closest('tr') ||
                         firstActiveCheckbox.parentElement.parentElement; // Fallback for some structures

        if (firstRow) {
            // --- STRATEGY 1: CLONING (Best for alignment) ---
            try {
                // Clone the entire row
                const toggleRow = firstRow.cloneNode(true);
                
                // Modify the clone to become our toggle row
                toggleRow.classList.add('field-mark-all');
                toggleRow.classList.remove('dynamic-form'); // Remove dynamic inline classes if any
                
                // --- CLEANUP AND BUTTON PLACEMENT ---
                
                // 1. Clear ALL children content to remove labels like "Active", "Home win odd", etc.
                // This ensures we keep the grid structure (divs/tds) but remove all text and inputs.
                Array.from(toggleRow.children).forEach(child => {
                    child.innerHTML = '';
                });

                // 2. Create the Button
                const toggleButton = document.createElement('button');
                toggleButton.type = 'button';
                toggleButton.id = 'toggle-all-odds-btn';
                toggleButton.className = 'btn btn-primary btn-sm'; // Bootstrap classes if available
                toggleButton.style.cursor = 'pointer';
                toggleButton.style.padding = '4px 12px';
                toggleButton.style.fontWeight = 'bold';
                toggleButton.style.fontSize = '12px';
                toggleButton.style.borderRadius = '4px';
                toggleButton.style.border = '1px solid #ccc';
                toggleButton.style.background = '#f0f0f0';
                toggleButton.style.color = '#333';
                toggleButton.textContent = 'Mark All';

                // 3. Place the Button
                // We want to place it in the second column (usually "Home win odd" column) to align nicely
                // If there's only 1 column, place in 1st.
                let targetCell = null;
                
                // Try to find the second visible layout element
                let visibleCount = 0;
                for (let i = 0; i < toggleRow.children.length; i++) {
                    const child = toggleRow.children[i];
                    if (child.style.display !== 'none' && child.type !== 'hidden') {
                        visibleCount++;
                        if (visibleCount === 2) {
                            targetCell = child;
                            break;
                        }
                    }
                }
                
                // Fallback to first child if no second child found
                if (!targetCell && toggleRow.children.length > 0) {
                    targetCell = toggleRow.children[0];
                }

                if (targetCell) {
                    targetCell.appendChild(toggleButton);
                }


                // Insert the cloned row before the first row
                firstRow.parentNode.insertBefore(toggleRow, firstRow);
                
                // Attach event listeners
                attachListeners(allActiveCheckboxes);
                return; // SUCCESS


            } catch (e) {
                console.error("Odds Toggle: Cloning failed, falling back to manual creation.", e);
                // Fall through to manual creation
            }
        }

        // --- STRATEGY 2: MANUAL CREATION (Fallback) ---
        // If cloning failed or row not found, create a simple div
        console.warn("Odds Toggle: Using fallback creation method.");
        
        const toggleContainer = document.createElement('div');
        toggleContainer.className = 'form-row field-mark-all'; 
        toggleContainer.style.marginBottom = '10px';
        toggleContainer.style.padding = '10px';
        toggleContainer.style.display = 'flex';
        toggleContainer.style.alignItems = 'center';
        toggleContainer.style.backgroundColor = '#f8f9fa'; // Light gray background to make it visible
        toggleContainer.style.border = '1px solid #dee2e6';
        toggleContainer.style.borderRadius = '4px';

        // Create the Button (Same as Strategy 1)
        const toggleButton = document.createElement('button');
        toggleButton.type = 'button';
        toggleButton.id = 'toggle-all-odds-btn';
        toggleButton.className = 'btn btn-primary btn-sm'; 
        toggleButton.style.cursor = 'pointer';
        toggleButton.style.padding = '4px 12px';
        toggleButton.style.fontWeight = 'bold';
        toggleButton.style.fontSize = '12px';
        toggleButton.style.borderRadius = '4px';
        toggleButton.style.border = '1px solid #ccc';
        toggleButton.style.background = '#f0f0f0';
        toggleButton.style.color = '#333';
        toggleButton.textContent = 'Mark All';
        
        toggleContainer.appendChild(toggleButton);

        // Find where to insert
        let container = null;
        let insertBeforeElement = null;

        // Try to find the fieldset
        const fieldset = firstActiveCheckbox.closest('fieldset');
        if (fieldset) {
            container = fieldset;
            // Insert after the h2 legend or at top
            const legend = fieldset.querySelector('h2');
            if (legend) {
                insertBeforeElement = legend.nextSibling;
            } else {
                insertBeforeElement = fieldset.firstChild;
            }
        } else if (firstRow) {
             container = firstRow.parentNode;
             insertBeforeElement = firstRow;
        } else {
             // Last resort: insert before the first checkbox's parent
             container = firstActiveCheckbox.parentElement.parentElement;
             insertBeforeElement = firstActiveCheckbox.parentElement;
        }

        if (container) {
            container.insertBefore(toggleContainer, insertBeforeElement);
            attachListeners(allActiveCheckboxes);
        } else {
            console.error("Odds Toggle: Could not find any container to insert toggle.");
        }
    }

    // Helper to attach listeners
    function attachListeners(allActiveCheckboxes) {
        const toggleButton = document.getElementById('toggle-all-odds-btn');
        if (!toggleButton) return;

        // Update button state
        function updateButtonState() {
            const allChecked = Array.from(allActiveCheckboxes).every(cb => cb.checked);
            
            if (allChecked) {
                toggleButton.textContent = 'Unmark All';
            } else {
                toggleButton.textContent = 'Mark All';
            }
        }

        // Initial check
        updateButtonState();

        // Master toggle event (Button Click)
        toggleButton.addEventListener('click', function(e) {
            e.preventDefault(); // Prevent form submission if inside a form
            
            const currentText = this.textContent;
            const shouldCheck = (currentText === 'Mark All');
            
            allActiveCheckboxes.forEach(cb => {
                cb.checked = shouldCheck;
            });
            
            // Update button text immediately
            updateButtonState();
        });

        // Individual checkboxes event
        allActiveCheckboxes.forEach(cb => {
            cb.addEventListener('change', updateButtonState);
        });
    }

    // Run on load
    initOddsToggle();
    
    // Also run on tab changes or dynamic content loading if possible
    // (Django admin usually doesn't reload via AJAX for tabs, but just in case)
    if (window.jQuery) {
        window.jQuery(document).on('formset:added', initOddsToggle);
    }
});
