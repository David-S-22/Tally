# Anomalies Frontend

## Overview

The frontend is a lightweight nginx container that serves the anomalies user
interface. The page is built with HTML, CSS, JavaScript, and HTMX, without a
separate frontend framework or build step. It displays detected anomalies,
related transaction details, review status, and feedback controls.
The page structure, styling, and interactions are kept in `public/anomalies.html`,
`public/anomalies.css`, and `public/anomalies.js`, respectively.

## User interface

The page describes its table as a list of flagged transactions, their suspected
reasons, and review status, with actions to confirm or dismiss unreviewed
findings. Rows load from the backend and refresh periodically using HTMX. The Transaction Date, Merchant, Confidence, and Status column headers are sortable;
the current sort is shown with SVG indicators and kept when refreshed rows
arrive. The Confidence column shows the model's certainty as a colored dot and
Low/Medium/High label (or a dash when no score is available). Blue, indigo, and
purple dot-labels distinguish confidence from the green Confirmed and red
Dismissed status badges. Unreviewed status badges are buttons styled as blue
pills with a trailing pencil icon and hover highlight; clicking one opens the review
dialog. Confidence indicators and status badges are centered
within their table cells. The anomaly table uses the available shell width,
with horizontal scrolling on narrow screens.
The review dialog shows the transaction details (a
locale-formatted date with weekday, merchant, amount, description, category
name, and confidence level with percentage) in two columns, followed by a
full-width suspected-reason row and a full-width sources row (the RAG
reference-document chips, or a dash when none) before collecting the user's
Confirm or Dismiss decision. Shared-server findings also require a reason,
which is sent with the decision; an error leaves the dialog open and the
finding unreviewed so the user can retry. Labels are styled as small uppercase
subheadings, with values emphasized in separate bordered cards. The dialog
animates when opening and closing, and pressing Escape cancels the review.

## Backend communication

Requests are sent through the `/anomalies-backend/` path:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/anomalies-backend/anomalies` | Loads and refreshes anomaly rows. |
| `POST` | `/anomalies-backend/anomalies/<id>/confirm` | Confirms an anomaly. |
| `POST` | `/anomalies-backend/anomalies/<id>/dismiss` | Dismisses an anomaly. |

HTMX handles the anomaly list requests and HTML fragment updates. JavaScript
uses `fetch` for review actions.

## Nginx and container configuration

Nginx serves files from `/app/public` and listens on the configured `PORT`,
which is `3004` in Docker Compose. Requests under `/anomalies-backend/` are
reverse-proxied to the URL provided by `ANOMALIES_BACKEND_URL`. The Dockerfile
uses `nginx:alpine`, copies the static page into the image, and installs the
templated nginx configuration. CSS and JavaScript are served through the
`/anomalies-assets/` route so their paths work both standalone and when the
page is embedded in the shared frontend. The stylesheet link is part of the
body fragment so it is retained when the shared shell loads the page via HTMX.
