# Debug Session: crm-popup-missing [OPEN]

## Symptom
- CRM dashboard message send completes, but no SweetAlert popup appears on the receiving user's page.

## Expected
- After sending from CRM dashboard, the recipient should immediately see a popup on any page.

## Hypotheses
- H1: The CRM dashboard send path is not emitting the expected websocket event.
- H2: The browser receives an event, but the payload shape does not satisfy the popup filter.
- H3: The target page is not loading the shared notification websocket script.
- H4: Broadcast send succeeds in DB/task flow, but does not create recipient-visible notification state.
- H5: Popup deduplication or gating logic is suppressing a valid event.

## Instrumentation Plan
- Add server-side debug reporting around CRM dashboard send.
- Add websocket consumer/client-side debug reporting for received notification events.
- Reproduce locally and inspect evidence before any logic fix.

## Evidence
- Pending

## Conclusion
- Pending
