# Multi-Parameter Profile: Equipment Creation Guide

This guide explains how to create equipment with **Multi-parameter** profile type in Django Admin and provides a worked example.

---

## 1. What is Multi-Parameter Profile?

**Multi-parameter** is one of the equipment profile types used for charging and time calculation:

| Profile Type     | Description |
|------------------|-------------|
| Sample-based     | Charge/time per sample. |
| Hour-based       | Charge/time per hour of use. |
| Sample + Element | Per sample with element selection (e.g. periodic table). |
| **Multi-parameter** | **Multiple “slot options” per user type** — each option has its own time per sample and charge per sample. The user selects one option (e.g. “1 Slot” or “2 Slots”) when booking. |

Use **Multi-parameter** when:

- The same equipment can be booked with different “packages” or slot options (e.g. 1 slot vs 2 slots).
- Each option has different **time per sample** and **charge per sample**.
- Options (and/or pricing) can differ by **user type** (Student, Faculty, Educational Institute, etc.).

---

## 2. Where to Create Equipment

1. Log in to **Django Admin**.
2. Go to **Equipment** → **Equipments**.
3. Click **Add Equipment**.

---

## 3. Step-by-Step: Creating Multi-Parameter Equipment

### Step 1: Basic Information

- **Name**: Full name of the equipment (e.g. “X-Ray Diffractometer”).
- **Code**: Unique short code (e.g. “XRD-01”).
- **Category**: Select the equipment category.
- **Equipment group** (optional): Group for grouping in the UI.
- **Profile type**: Select **Multi-parameter**.
- **Description**, **Status**, **Location**, etc.: Fill as needed.

### Step 2: Slot Configuration (optional but recommended)

- **Slot duration (minutes)**: e.g. 30.
- **Slots per day**: e.g. 12.
- **Reschedule hours threshold**: e.g. 48.

### Step 3: Slot Options Configuration (Multi-Parameter Definitions)

Expand the **“Slot Options Configuration”** (or “Multi-Parameter Definitions”) section.

Here you define one or more **slot options per user type**. Each row has:

| Field                 | Description |
|-----------------------|-------------|
| **User type**         | Student, Faculty, Educational Institute, Govt R&D Center, or Industry. |
| **Slot Option Name**  | Display name (e.g. “1 Slot”, “2 Slots”, “Half Day”). |
| **Slot Option Code**  | Short code used in booking/calculation (e.g. “1”, “2”, “HALF”). Must be unique per equipment + user type. |
| **Time per Sample (minutes)** | Minutes allocated per sample for this option. |
| **Charge per Sample** | Price per sample for this option. |
| **Is active**         | Check to enable the option. |

Add as many rows as you need (one row = one option for one user type). You can add different options for different user types (e.g. Student: 1 Slot / 2 Slots; Faculty: 1 Slot / 2 Slots with different charges).

### Step 4: Charge Profiles

In **Charge profiles**, define pricing per user type. For Multi-parameter, the system uses the **Slot Options Configuration** (time and charge per sample) for calculation; ensure the user types you use in Slot Options match the charge profile user types as needed.

### Step 5: Slot Masters

Define **Slot Masters** (slot number, name, open time, close time) so that the equipment has bookable slots.

### Step 6: Other Sections (optional)

- **Equipment specifications**, **Managers**, **Operators**, **Dynamic input fields**, etc., as per your setup.

Save the equipment.

---

## 4. Example: Multi-Parameter Equipment

### Scenario

- **Equipment**: “X-Ray Diffractometer”.
- **User types**: Student and Faculty.
- **Options**: “1 Slot” and “2 Slots” for each user type, with different time and charge per sample.

### 4.1 Basic Information (example)

| Field         | Value                |
|---------------|----------------------|
| Name          | X-Ray Diffractometer |
| Code          | XRD-01               |
| Category      | (e.g. Characterization) |
| Profile type  | **Multi-parameter**  |
| Status        | ACTIVE               |
| Location      | Block A, Lab 101     |

### 4.2 Slot Options Configuration (example rows)

**For User type: Student**

| User type | Slot Option Name | Slot Option Code | Time per Sample (minutes) | Charge per Sample | Is active |
|-----------|-------------------|------------------|----------------------------|-------------------|-----------|
| Student   | 1 Slot            | 1                | 60                         | 100.00            | ✓         |
| Student   | 2 Slots           | 2                | 120                        | 180.00            | ✓         |

**For User type: Faculty**

| User type | Slot Option Name | Slot Option Code | Time per Sample (minutes) | Charge per Sample | Is active |
|-----------|-------------------|------------------|----------------------------|-------------------|-----------|
| Faculty   | 1 Slot            | 1                | 60                         | 150.00            | ✓         |
| Faculty   | 2 Slots           | 2                | 120                        | 270.00            | ✓         |

So:

- **Student** can choose “1 Slot” (60 min, ₹100) or “2 Slots” (120 min, ₹180).
- **Faculty** can choose “1 Slot” (60 min, ₹150) or “2 Slots” (120 min, ₹270).

### 4.3 Slot Masters (example)

| Slot number | Slot name  | Open time | Close time | Is active |
|-------------|------------|-----------|------------|-----------|
| 1           | Morning-1  | 09:00     | 09:30      | ✓         |
| 2           | Morning-2  | 09:30     | 10:00      | ✓         |
| …           | …          | …         | …          | ✓         |

(Add as many slots as needed for the day.)

### 4.4 How It Works for the User

1. User selects the equipment (e.g. X-Ray Diffractometer).
2. They see a **radio field** (or similar) built from your **Slot Option Name** and **Slot Option Code** for their user type (e.g. “1 Slot”, “2 Slots”).
3. They choose one option (e.g. “2 Slots”).
4. System uses the matching **Time per Sample** and **Charge per Sample** for that user type and option to compute required time and charge.
5. User selects actual time slots (e.g. two 30-minute slots) and completes the booking.

---

## 5. Important Notes

- **Unique (equipment, user_type, param_code)**  
  For the same equipment and user type, each **Slot Option Code** must be unique (e.g. only one “1” for Student, one “2” for Student).

- **Param code**  
  Use short, stable codes (e.g. `1`, `2`, `HALF`). They are used in calculations and in dynamic input/options.

- **Time per sample**  
  This is the total minutes allocated for that option (e.g. 60 for “1 Slot”, 120 for “2 Slots”), not per physical slot.

- **Charge profiles**  
  For Multi-parameter, the per-sample time and charge come from **Slot Options Configuration**. Charge profiles still define which user types are allowed and any other global pricing rules your app uses.

- **Dynamic input fields**  
  If you use a “number of slots” or “slot option” dynamic field, it should match the **param_code** (and optionally user type) so the backend can resolve the correct MultiParamDefinition row for time and charge.

- **Add view and inlines**  
  On **Add Equipment**, the Slot Options Configuration inline is always present so the management form is submitted correctly. You can leave it empty or add rows; expand the section to add options before saving.

---

## 6. Summary Checklist

- [ ] Profile type set to **Multi-parameter**.
- [ ] At least one **Slot Options Configuration** row per user type you want to support (param_name, param_code, unit_time_minutes, unit_charge).
- [ ] **Slot Option Code** unique per (equipment, user_type).
- [ ] **Slot Masters** defined for bookable slots.
- [ ] **Charge profiles** and **user types** aligned with Slot Options and your business rules.

For more detail on models and admin, see the `equipment` app: `Equipment` (profile_type), `MultiParamDefinition` (slot options), and `SlotMaster`.
