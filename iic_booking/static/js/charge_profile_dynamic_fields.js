/**
 * Nest Dynamic Input Field inline rows under each Charge Profile stacked inline.
 *
 * Django cannot nest formsets, so rows stay in the input_fields formset for POST,
 * but are visually moved under the matching charge-profile user type.
 * PI alias rows (__pi__:...) share fields with the underlying user type and show a note.
 */
(function ($) {
  "use strict";

  var PI_PREFIX = "__pi__:";

  function decodeUserType(raw) {
    var v = (raw || "").toString();
    if (v.indexOf(PI_PREFIX) === 0) {
      return { userType: v.slice(PI_PREFIX.length), isPi: true };
    }
    return { userType: v, isPi: false };
  }

  function findDynamicGroup() {
    var $byId = $("#input_fields-group");
    if ($byId.length) return $byId;
    return $(".inline-group.js-dynamic-input-fields-inline, .inline-group").filter(function () {
      var t = ($(this).find("> h2, .inline-heading").first().text() || "").toLowerCase();
      return t.indexOf("dynamic input") >= 0;
    }).first();
  }

  function findChargeGroup() {
    var $byId = $("#charge_profiles-group");
    if ($byId.length) return $byId;
    return $(".inline-group").filter(function () {
      var t = ($(this).find("> h2, .inline-heading").first().text() || "").toLowerCase();
      return t.indexOf("charge profile") >= 0 && t.indexOf("pi charge") < 0;
    }).first();
  }

  function ensureNest($related) {
    var $nest = $related.find("> .cp-dynamic-fields-nest");
    if ($nest.length) return $nest;
    $nest = $(
      '<div class="cp-dynamic-fields-nest" style="margin:12px 0;padding:12px;border:1px solid #ccd0d4;background:#f8f9fa;border-radius:4px;">' +
        '<div class="cp-dynamic-fields-header" style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;">' +
          "<div>" +
            '<strong>Dynamic input fields</strong>' +
            '<p class="help" style="margin:4px 0 0;">Fields A–Z for this user type (used in formulas and booking). User type is assumed from this charge profile.</p>' +
          "</div>" +
          '<button type="button" class="button cp-add-dynamic-field">Add dynamic field</button>' +
        "</div>" +
        '<div class="cp-dynamic-fields-pi-note" style="display:none;margin-bottom:8px;font-size:12px;color:#666;"></div>' +
        '<div class="cp-dynamic-fields-rows"></div>' +
      "</div>"
    );
    // Insert before Delete checkbox row when present, else append.
    var $deleteRow = $related.find(".form-row.field-DELETE, .delete").last();
    if ($deleteRow.length) {
      $nest.insertBefore($deleteRow.closest(".form-row").length ? $deleteRow.closest(".form-row") : $deleteRow);
    } else {
      $related.append($nest);
    }
    return $nest;
  }

  function dynamicRows($dynGroup) {
    // TabularInline: tbody > tr.form-row (or tr.has_original / tr.empty-form)
    return $dynGroup.find("table tbody tr").filter(function () {
      var $tr = $(this);
      if ($tr.hasClass("empty-form") || $tr.hasClass("add-row")) return false;
      // Skip header-like rows without inputs
      return $tr.find('input, select, textarea').length > 0;
    });
  }

  function rowUserType($row) {
    var $ut = $row.find('input[name$="-user_type"], select[name$="-user_type"]').first();
    return ($ut.val() || "").toString();
  }

  function setRowUserType($row, userType) {
    var $ut = $row.find('input[name$="-user_type"], select[name$="-user_type"]').first();
    if ($ut.length) $ut.val(userType).trigger("change");
  }

  function regroup() {
    var $dyn = findDynamicGroup();
    var $charges = findChargeGroup();
    if (!$dyn.length || !$charges.length) return;

    // Soft-hide standalone dynamic section (keep management form / empty-form in DOM).
    $dyn.addClass("cp-dynamic-fields-source");
    $dyn.find("> h2, .inline-heading").first().hide();
    $dyn.css({
      border: "none",
      margin: 0,
      padding: 0,
      height: 0,
      overflow: "hidden",
      opacity: 0,
      position: "absolute",
      left: "-9999px",
    });

    // Hide user_type column header if still visible.
    $dyn.find("thead th").each(function () {
      var t = ($(this).text() || "").toLowerCase();
      if (t.indexOf("user type") >= 0 || t.indexOf("user_type") >= 0) {
        $(this).hide();
      }
    });

    var $relateds = $charges.find(".inline-related").not(".empty-form");
    $relateds.each(function () {
      ensureNest($(this));
    });

    // Clear previous placements (move rows back to source tbody first).
    var $tbody = $dyn.find("table tbody").first();
    $charges.find(".cp-dynamic-fields-rows tr").each(function () {
      $tbody.append(this);
    });

    // Build map userType -> nest rows container (STANDARD only).
    var nestByUserType = {};
    $relateds.each(function () {
      var $rel = $(this);
      var $sel = $rel.find('select[name$="-user_type"]').first();
      var decoded = decodeUserType($sel.val());
      var $nest = ensureNest($rel);
      var $rows = $nest.find(".cp-dynamic-fields-rows");
      var $note = $nest.find(".cp-dynamic-fields-pi-note");
      var $add = $nest.find(".cp-add-dynamic-field");
      var $headerHelp = $nest.find(".cp-dynamic-fields-header .help");

      if (decoded.isPi) {
        var hasStandard = false;
        $relateds.each(function () {
          var other = decodeUserType(
            $(this).find('select[name$="-user_type"]').first().val()
          );
          if (!other.isPi && other.userType === decoded.userType) hasStandard = true;
        });
        if (hasStandard) {
          $note
            .show()
            .text(
              "Dynamic input fields for PI rates are shared with the matching standard user type (" +
                (decoded.userType || "faculty") +
                "). Edit them on the standard charge profile row for that user type."
            );
          $add.hide();
          $rows.hide();
          if ($headerHelp.length) {
            $headerHelp.text(
              "PI IIT Faculty uses the same dynamic fields as IITR Faculty (no separate field set)."
            );
          }
          return;
        }
        // No standard row yet — allow editing fields here (still keyed by real user_type).
        $note
          .show()
          .text(
            "No standard charge profile for this user type yet. Fields added here apply to user type “" +
              (decoded.userType || "faculty") +
              "” (shared if you later add a standard row)."
          );
        $add.show();
        $rows.show();
        if (decoded.userType) {
          nestByUserType[decoded.userType] = $rows;
        }
        return;
      }

      $note.hide().text("");
      $add.show();
      $rows.show();
      if ($headerHelp.length) {
        $headerHelp.text(
          "Fields A–Z for this user type (used in formulas and booking). User type is assumed from this charge profile."
        );
      }
      if (decoded.userType) {
        nestByUserType[decoded.userType] = $rows;
      }
    });

    // Place each dynamic row under matching user type; orphan rows stay in a fallback.
    var $fallback = $charges.find(".cp-dynamic-fields-orphan");
    if (!$fallback.length) {
      $fallback = $(
        '<div class="cp-dynamic-fields-orphan" style="margin:12px 0;padding:12px;border:1px dashed #ba2121;background:#fff5f5;">' +
          "<strong>Unassigned dynamic fields</strong>" +
          '<p class="help">These rows have no matching charge profile user type yet. Add a charge profile for that user type, or set user_type.</p>' +
          '<div class="cp-dynamic-fields-rows"></div>' +
        "</div>"
      );
      $charges.append($fallback);
    }
    var $fallbackRows = $fallback.find(".cp-dynamic-fields-rows");
    $fallbackRows.empty();

    dynamicRows($dyn).each(function () {
      var $row = $(this);
      // Skip if this is still the template empty-form
      if (($row.attr("id") || "").indexOf("empty") >= 0) return;
      var ut = rowUserType($row);
      // Hide user_type cell
      $row.find(".field-user_type, td.field-user_type").hide();
      var $target = (ut && nestByUserType[ut]) || $fallbackRows;
      // Wrap tabular row in a table if needed
      if ($target.is(".cp-dynamic-fields-rows") && !$target.is("table") && $target.children("table").length === 0) {
        var $tbl = $('<table class="tabular cp-nested-dyn-table" style="width:100%;"><tbody></tbody></table>');
        $target.append($tbl);
      }
      var $body = $target.is("table") ? $target.find("tbody") : $target.find("table tbody").first();
      if (!$body.length) {
        $target.append($row);
      } else {
        $body.append($row);
      }
    });

    if ($fallbackRows.find("tr").length === 0) {
      $fallback.hide();
    } else {
      $fallback.show();
    }
  }

  function addFieldForUserType(userType) {
    var $dyn = findDynamicGroup();
    if (!$dyn.length) return;
    var $add = $dyn.find(".add-row a, .add-row button, a.add-related").first();
    if (!$add.length) {
      // Django tabular add link
      $add = $dyn.find(".tabular .add-row a").first();
    }
    if ($add.length) {
      $add.get(0).click();
    }
    // After Django adds empty form, set user_type and regroup.
    window.setTimeout(function () {
      var $rows = dynamicRows($dyn);
      var $last = $rows.last();
      if ($last.length && userType) {
        setRowUserType($last, userType);
      }
      regroup();
    }, 50);
  }

  $(document).ready(function () {
    var $charges = findChargeGroup();
    if (!$charges.length) return;

    regroup();

    $(document).on("change", '#charge_profiles-group select[name$="-user_type"]', function () {
      regroup();
    });

    $(document).on("click", ".cp-add-dynamic-field", function (e) {
      e.preventDefault();
      var $rel = $(this).closest(".inline-related");
      var raw = $rel.find('select[name$="-user_type"]').val();
      var decoded = decodeUserType(raw);
      if (decoded.isPi || !decoded.userType) return;
      addFieldForUserType(decoded.userType);
    });

    // When Django formset adds charge profile rows
    $(document).on("formset:added", function () {
      window.setTimeout(regroup, 30);
    });

    // Periodic light refresh after inline add links
    $(document).on("click", "#charge_profiles-group .add-row a", function () {
      window.setTimeout(regroup, 80);
    });
  });
})(django.jQuery);
