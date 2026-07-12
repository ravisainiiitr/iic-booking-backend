# OpenAPI Inventory QA Checklist

Use this checklist to validate that inventory APIs are exposed and documented correctly in Swagger/OpenAPI UI.

## Preconditions

- Backend server is running.
- Auth token is available for an admin-panel user.
- At least one equipment and one inventory item exists.

## Swagger UI Presence Checks

- [ ] Open Swagger UI page.
- [ ] Confirm inventory endpoints are visible under API paths:
  - [ ] `GET /api/inventory/items/`
  - [ ] `GET /api/inventory/equipments/{equipment_id}/stock/`
  - [ ] `GET /api/inventory/requests/`
  - [ ] `POST /api/inventory/requests/`
  - [ ] `POST /api/inventory/requests/{request_id}/decide/`
  - [ ] `POST /api/inventory/requests/{request_id}/issue/`

## Schema Validation Checks

- [ ] `GET /api/inventory/items/` shows query parameter `active_only`.
- [ ] `GET /api/inventory/requests/` shows query parameters `status` and `equipment_id`.
- [ ] `POST /api/inventory/requests/` request body schema renders inventory request fields.
- [ ] `POST /api/inventory/requests/{request_id}/decide/` request body contains `action` and `decision_note`.
- [ ] `POST /api/inventory/requests/{request_id}/issue/` request body contains `lines[]` with `line_id` and `issue_qty`.
- [ ] Response schema for issue endpoint shows `request` and `transactions`.

## Try-It-Out Functional Checks

- [ ] Authorize with valid token in Swagger.
- [ ] Call `GET /api/inventory/items/` and confirm `200`.
- [ ] Call `GET /api/inventory/equipments/{equipment_id}/stock/` with valid equipment id and confirm `200`.
- [ ] Create request via `POST /api/inventory/requests/` and confirm `201` with generated `request_no`.
- [ ] Decide request via `POST /api/inventory/requests/{request_id}/decide/` and confirm status updates.
- [ ] Issue request via `POST /api/inventory/requests/{request_id}/issue/` and confirm transaction records in response.

## Negative Case Checks

- [ ] Call any inventory endpoint without token and confirm auth error.
- [ ] Create request for equipment not managed by current OIC and confirm `403`.
- [ ] Issue with `issue_qty` > pending approved quantity and confirm validation error.
- [ ] Decide request with invalid `action` and confirm validation error.

## Evidence to Capture

- [ ] Screenshot: inventory endpoint list in Swagger.
- [ ] Screenshot: POST create request schema and successful response.
- [ ] Screenshot: POST issue request schema and successful response.
- [ ] Screenshot: one validation error response (negative test).

## Optional Cross-check in Admin

- [ ] Verify issued transaction appears in Inventory Transaction admin.
- [ ] Verify stock quantity changed in Equipment Item Stock admin.
- [ ] Verify request status became `PARTIALLY_FULFILLED` or `FULFILLED` as expected.
