(function () {
  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
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
    const confirmInput = form.querySelector('input[name="confirm_duplicate_email"]');
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
      if (!checkUrl || typeof Swal === "undefined") {
        return;
      }

      event.preventDefault();

      const excludeUserId =
        (form.querySelector('input[name="email_warning_exclude_id"]') || {}).value || "";

      let usage;
      try {
        usage = await fetchEmailUsage(checkUrl, email, excludeUserId);
      } catch (error) {
        form.dataset.duplicateEmailReady = "1";
        form.submit();
        return;
      }

      if (usage && usage.exists && confirmInput.value !== "1") {
        const matches = Array.isArray(usage.matches) ? usage.matches : [];
        const matchLines = matches
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
            "<p><strong>" + escapeHtml(email) + "</strong></p>" +
            "<p>Do you want to continue assigning this email to another username?</p>" +
            matchLines,
          showCancelButton: true,
          confirmButtonText: "Proceed",
          cancelButtonText: "Cancel",
        });

        if (!result.isConfirmed) {
          confirmInput.value = "";
          return;
        }

        confirmInput.value = "1";
      }

      const originalEmailInput = form.querySelector('input[name="original_email"]');
      const hasLinkedCashiersInput = form.querySelector('input[name="has_linked_cashiers"]');
      const syncCashiersInput = form.querySelector('input[name="sync_cashier_emails"]');
      const originalEmail = ((originalEmailInput || {}).value || "").trim().toLowerCase();

      if (
        syncCashiersInput &&
        hasLinkedCashiersInput &&
        hasLinkedCashiersInput.value === "1" &&
        originalEmail &&
        originalEmail !== email.toLowerCase()
      ) {
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
          return;
        }

        syncCashiersInput.value = syncResult.isConfirmed ? "1" : "0";
      }

      form.dataset.duplicateEmailReady = "1";
      form.submit();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form").forEach(bindDuplicateEmailForm);
  });
})();
