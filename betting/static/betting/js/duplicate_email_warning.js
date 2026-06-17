(function () {
  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function ensureHiddenInput(form, fieldName) {
    let input = form.querySelector('input[name="' + fieldName + '"]');
    if (input) {
      return input;
    }

    input = document.createElement("input");
    input.type = "hidden";
    input.name = fieldName;
    form.appendChild(input);
    return input;
  }

  function formHasServerDuplicateEmailError(form) {
    const errorSelectors = [".errorlist", ".errors", ".invalid-feedback", ".text-danger", ".errornote"];
    return errorSelectors.some(function (selector) {
      return Array.from(form.querySelectorAll(selector)).some(function (node) {
        const text = (node.textContent || "").trim();
        return /This email is already assigned to another user\.\s*Confirm to continue\./i.test(text);
      });
    });
  }

  async function confirmDuplicateEmail(email, matches) {
    const safeEmail = escapeHtml(email);
    const items = Array.isArray(matches) ? matches : [];

    if (typeof Swal !== "undefined") {
      const matchLines = items
        .map(function (match) {
          const username = escapeHtml(match.username || "-");
          const userType = escapeHtml(match.user_type || "user");
          return "<div class=\"small text-start\">" + username + " (" + userType + ")</div>";
        })
        .join("");

      const result = await Swal.fire({
        icon: "warning",
        title: "Email Already Exists",
        html:
          "<p>A user is already registered with this email address:</p>" +
          "<p><strong>" + safeEmail + "</strong></p>" +
          "<p>Do you want to continue assigning this email to another username?</p>" +
          matchLines,
        showCancelButton: true,
        confirmButtonText: "Proceed",
        cancelButtonText: "Cancel",
      });
      return Boolean(result.isConfirmed);
    }

    const matchSummary = items.length
      ? "\n\nExisting matches:\n" +
        items
          .map(function (match) {
            return "- " + (match.username || "-") + " (" + (match.user_type || "user") + ")";
          })
          .join("\n")
      : "";

    return window.confirm(
      "This email is already assigned to another user.\n\nEmail: " +
        email +
        "\n\nDo you want to continue?" +
        matchSummary
    );
  }

  async function confirmCashierSync() {
    if (typeof Swal !== "undefined") {
      const syncResult = await Swal.fire({
        icon: "question",
        title: "Linked Cashiers",
        text: "This Agent has linked Cashiers. Do you also want to update the Cashiers' email addresses to match the Agent?",
        showDenyButton: true,
        showCancelButton: true,
        confirmButtonText: "Yes",
        denyButtonText: "No",
        cancelButtonText: "Cancel",
      });

      if (syncResult.isDismissed && !syncResult.isConfirmed && !syncResult.isDenied) {
        return { proceed: false, sync: false };
      }

      return { proceed: true, sync: Boolean(syncResult.isConfirmed) };
    }

    const shouldSync = window.confirm(
      "This Agent has linked Cashiers.\n\nClick OK to also update the linked Cashiers' email addresses to match the Agent.\nClick Cancel to keep linked Cashier emails unchanged."
    );
    return { proceed: true, sync: shouldSync };
  }

  async function fetchEmailUsage(url, email, excludeUserId) {
    const params = new URLSearchParams();
    params.set("email", email);
    if (excludeUserId) {
      params.set("exclude_user_id", excludeUserId);
    }
    const response = await fetch(url + "?" + params.toString(), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error("Unable to validate email usage.");
    }
    return response.json();
  }

  function bindDuplicateEmailForm(form) {
    if (!form || form.dataset.duplicateEmailBound === "1") {
      return;
    }

    const emailInput = form.querySelector('input[name="email"]');
    const confirmInput = ensureHiddenInput(form, "confirm_duplicate_email");
    if (!emailInput || !confirmInput) {
      return;
    }

    form.dataset.duplicateEmailBound = "1";
    form.addEventListener("submit", async function (event) {
      if (form.dataset.duplicateEmailReady === "1") {
        return;
      }

      const email = (emailInput.value || "").trim();
      if (!email) {
        return;
      }

      const checkUrl = form.dataset.emailCheckUrl || window.duplicateEmailCheckUrl || "/email-usage-check/";
      event.preventDefault();

      const excludeUserId =
        (ensureHiddenInput(form, "email_warning_exclude_id") || {}).value || "";
      const hasServerDuplicateError = formHasServerDuplicateEmailError(form);

      let usage;
      if (!hasServerDuplicateError && checkUrl) {
        try {
          usage = await fetchEmailUsage(checkUrl, email, excludeUserId);
        } catch (error) {
          usage = null;
        }
      }

      const shouldConfirmDuplicate =
        confirmInput.value !== "1" &&
        (hasServerDuplicateError || (usage && usage.exists));
      if (shouldConfirmDuplicate) {
        const confirmed = await confirmDuplicateEmail(email, (usage && usage.matches) || []);
        if (!confirmed) {
          confirmInput.value = "";
          return;
        }

        confirmInput.value = "1";
      }

      const originalEmailInput = ensureHiddenInput(form, "original_email");
      const hasLinkedCashiersInput = ensureHiddenInput(form, "has_linked_cashiers");
      const syncCashiersInput = ensureHiddenInput(form, "sync_cashier_emails");
      const originalEmail = ((originalEmailInput || {}).value || "").trim().toLowerCase();

      if (
        syncCashiersInput &&
        hasLinkedCashiersInput &&
        hasLinkedCashiersInput.value === "1" &&
        originalEmail &&
        originalEmail !== email.toLowerCase()
      ) {
        const syncResult = await confirmCashierSync();
        if (!syncResult.proceed) {
          return;
        }

        syncCashiersInput.value = syncResult.sync ? "1" : "0";
      }

      form.dataset.duplicateEmailReady = "1";
      form.submit();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form").forEach(bindDuplicateEmailForm);
  });
})();
