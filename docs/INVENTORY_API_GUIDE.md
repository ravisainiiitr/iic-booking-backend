# Inventory API Guide

This document describes the inventory endpoints added for equipment-wise inventory management.

Base path prefix is `/api/`.

Swagger verification checklist: `docs/OPENAPI_INVENTORY_SCREENSHOTS.md`

## Auth and Access

- All endpoints require authenticated user.
- Admin panel users can access all inventory endpoints.
- OIC / temporary OIC can create and list requests for their associated equipment.

## Endpoints

### 1) List Inventory Items

- **Method**: `GET`
- **Path**: `/api/inventory/items/`
- **Query params**:
  - `active_only` (optional, default `1`)

Response:

```json
{
  "items": [
    {
      "item_id": 1,
      "item_code": "CHEM-ACID-001",
      "name": "Nitric Acid",
      "category": "CONSUMABLE",
      "uom": "L",
      "specification": "AR grade",
      "active": true,
      "created_at": "2026-03-24T10:00:00Z",
      "updated_at": "2026-03-24T10:00:00Z"
    }
  ]
}
```

### 2) Equipment Stock + Configured Items

- **Method**: `GET`
- **Path**: `/api/inventory/equipments/{equipment_id}/stock/`

Response:

```json
{
  "equipment_id": 12,
  "configured_items": [
    {
      "id": 3,
      "equipment": 12,
      "item": {
        "item_id": 1,
        "item_code": "CHEM-ACID-001",
        "name": "Nitric Acid",
        "category": "CONSUMABLE",
        "uom": "L",
        "specification": "AR grade",
        "active": true,
        "created_at": "2026-03-24T10:00:00Z",
        "updated_at": "2026-03-24T10:00:00Z"
      },
      "min_level": "2.000",
      "max_level": "20.000",
      "reorder_level": "5.000",
      "critical_level": "2.000",
      "default_store_location": "Store A",
      "is_enabled": true,
      "created_at": "2026-03-24T10:00:00Z",
      "updated_at": "2026-03-24T10:00:00Z"
    }
  ],
  "stock": [
    {
      "id": 8,
      "equipment": 12,
      "item": {
        "item_id": 1,
        "item_code": "CHEM-ACID-001",
        "name": "Nitric Acid",
        "category": "CONSUMABLE",
        "uom": "L",
        "specification": "AR grade",
        "active": true,
        "created_at": "2026-03-24T10:00:00Z",
        "updated_at": "2026-03-24T10:00:00Z"
      },
      "current_qty": "11.000",
      "updated_at": "2026-03-24T10:00:00Z"
    }
  ]
}
```

### 3) List Inventory Requests

- **Method**: `GET`
- **Path**: `/api/inventory/requests/`
- **Query params**:
  - `status` (optional)
  - `equipment_id` (optional)

Response:

```json
{
  "requests": [
    {
      "request_id": 15,
      "request_no": "INVR-20260324-0001",
      "equipment": 12,
      "requested_by": 55,
      "request_type": "MIXED",
      "status": "SUBMITTED",
      "justification": "Required for next batch",
      "required_by_date": "2026-03-31",
      "submitted_at": "2026-03-24T09:00:00Z",
      "decision_by": null,
      "decision_at": null,
      "decision_note": "",
      "created_at": "2026-03-24T09:00:00Z",
      "updated_at": "2026-03-24T09:00:00Z",
      "lines": []
    }
  ]
}
```

### 4) Create Inventory Request

- **Method**: `POST`
- **Path**: `/api/inventory/requests/`
- **Access**: OIC or active temporary OIC of the equipment

Request body:

```json
{
  "equipment": 12,
  "request_type": "MIXED",
  "status": "SUBMITTED",
  "justification": "Consumables and one replacement probe required",
  "required_by_date": "2026-03-31",
  "lines": [
    {
      "item": 1,
      "requested_qty": "5.000",
      "approved_qty": "0.000",
      "issued_qty": "0.000",
      "estimated_unit_cost": "250.00",
      "remarks": "For weekly operation"
    },
    {
      "item": 9,
      "requested_qty": "1.000",
      "approved_qty": "0.000",
      "issued_qty": "0.000",
      "estimated_unit_cost": "12000.00",
      "remarks": "Probe damaged"
    }
  ]
}
```

### 5) Approve/Reject Request

- **Method**: `POST`
- **Path**: `/api/inventory/requests/{request_id}/decide/`
- **Access**: admin panel users

Request body:

```json
{
  "action": "APPROVE",
  "decision_note": "Approved as per budget."
}
```

- `action` allowed values: `APPROVE`, `REJECT`

### 6) Issue Against Request

- **Method**: `POST`
- **Path**: `/api/inventory/requests/{request_id}/issue/`
- **Access**: admin panel users

Request body:

```json
{
  "lines": [
    {
      "line_id": 101,
      "issue_qty": "3.000",
      "remarks": "Issued from central store"
    },
    {
      "line_id": 102,
      "issue_qty": "1.000",
      "serial_no": "PRB-2026-0091",
      "issued_to": 55,
      "condition_on_issue": "New",
      "remarks": "Issued as replacement"
    }
  ]
}
```

Response includes updated request and created issue transactions.

## Error Response Format

Endpoints return a simple error payload:

```json
{
  "error": "Human readable message"
}
```

---

## Postman Collection Snippet

Import this as raw JSON in Postman (or append to your existing collection):

```json
{
  "info": {
    "name": "IIC Inventory APIs",
    "_postman_id": "f6eaf276-447b-44f4-bf6e-5a1a35a2278f",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "Inventory - List Items",
      "request": {
        "method": "GET",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          }
        ],
        "url": "{{base_url}}/api/inventory/items/?active_only=1"
      }
    },
    {
      "name": "Inventory - Equipment Stock",
      "request": {
        "method": "GET",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          }
        ],
        "url": "{{base_url}}/api/inventory/equipments/{{equipment_id}}/stock/"
      }
    },
    {
      "name": "Inventory - List Requests",
      "request": {
        "method": "GET",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          }
        ],
        "url": "{{base_url}}/api/inventory/requests/"
      }
    },
    {
      "name": "Inventory - Create Request",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          },
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n  \"equipment\": {{equipment_id}},\n  \"request_type\": \"MIXED\",\n  \"status\": \"SUBMITTED\",\n  \"justification\": \"Required for operations\",\n  \"required_by_date\": \"2026-03-31\",\n  \"lines\": [\n    {\n      \"item\": 1,\n      \"requested_qty\": \"5.000\",\n      \"approved_qty\": \"0.000\",\n      \"issued_qty\": \"0.000\"\n    }\n  ]\n}"
        },
        "url": "{{base_url}}/api/inventory/requests/"
      }
    },
    {
      "name": "Inventory - Decide Request",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          },
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n  \"action\": \"APPROVE\",\n  \"decision_note\": \"Approved\"\n}"
        },
        "url": "{{base_url}}/api/inventory/requests/{{request_id}}/decide/"
      }
    },
    {
      "name": "Inventory - Issue Request",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Authorization",
            "value": "Token {{auth_token}}"
          },
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n  \"lines\": [\n    {\n      \"line_id\": 101,\n      \"issue_qty\": \"1.000\",\n      \"remarks\": \"Issued\"\n    }\n  ]\n}"
        },
        "url": "{{base_url}}/api/inventory/requests/{{request_id}}/issue/"
      }
    }
  ],
  "variable": [
    {
      "key": "base_url",
      "value": "http://127.0.0.1:8000"
    },
    {
      "key": "auth_token",
      "value": ""
    },
    {
      "key": "equipment_id",
      "value": "12"
    },
    {
      "key": "request_id",
      "value": "1"
    }
  ]
}
```
