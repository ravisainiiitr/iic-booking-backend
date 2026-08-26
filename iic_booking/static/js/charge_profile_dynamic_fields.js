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
  var regroupTimer = null;

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
    return $(".inline-group.js-dynamic-input-fields-inline, .inline-group")
      .filter(function () {
        var t = ($(this).find("> h2, .inline-heading").first().text() || "").toLowerCase();
        return t.indexOf("dynamic input") >= 0;
      })
      .first();
  }

  function findChargeGroup() {
    var $byId = $("#charge_profiles-group");
    if ($byId.length) return $byId;
    return $(".inline-group")
      .filter(function () {
        var t = ($(this).find("> h2, .inline-heading").first().text() || "").toLowerCase();
        return t.indexOf("charge profile") >= 0 && t.indexOf("pi charge") < 0;
      })
      .first();
  }

  function fieldsetOf($related) {
    var $fs = $related.children("fieldset").first();
    if ($fs.length) return $fs;
    return $related.find("fieldset").first();
  }

  /**
   * One nest per charge-profile card, immediately AFTER User type / Profile type / Is active.
   * Never insert near the h3 .delete control (that caused duplicate sections).
   */
  function ensureNest($related) {
    var $existing = $related.find(".cp-dynamic-fields-nest");
    if ($existing.length > 1) {
      $existing.slice(1).remove();
      $existing = $related.find(".cp-dynamic-fields-nest").first();
    }
    if ($existing.length) {
      placeNestAfterProfileControls($related, $existing);
      return $existing;
    }

    var $nest = $(
      '<div class="cp-dynamic-fields-nest" style="margin:12px 0;padding:12px;border:1px solid #ccd0d4;background:#f8f9fa;border-radius:4px;clear:both;">' +
        '<div class="cp-dynamic-fields-header" style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;">' +
          "<div>" +
            "<strong>Dynamic input fields</strong>" +
            '<p class="help" style="margin:4px 0 0;">Fields A–Z for this user type (used in formulas and booking). User type is assumed from this charge profile.</p>' +
          "</div>" +
          '<button type="button" class="button cp-add-dynamic-field">Add dynamic field</button>' +
        "</div>" +
        '<div class="cp-dynamic-fields-pi-note" style="display:none;margin-bottom:8px;font-size:12px;color:#666;"></div>' +
        '<div class="cp-dynamic-fields-rows"><table class="tabular cp-nested-dyn-table" style="width:100%;"><thead></thead><tbody></tbody></table></div>' +
      "</div>"
    );
    placeNestAfterProfileControls($related, $nest);
    return $nest;
  }

  function findFormRow($related, fieldName) {
    var $row = $related.find(".form-row.field-" + fieldName).first();
    if ($row.length) return $row;
    return $related
      .find(".field-" + fieldName)
      .closest(".form-row")
      .first();
  }

  function placeNestAfterProfileControls($related, $nest) {
    var $fs = fieldsetOf($related);
    var $host = $fs.length ? $fs : $related;

    // Desired order: User type → Profile type → Is active → Dynamic fields → rest
    var $anchor =
      findFormRow($related, "is_active") ||
      findFormRow($related, "profile_type") ||
      findFormRow($related, "user_type");

    if ($anchor && $anchor.length) {
      $nest.insertAfter($anchor);
      return;
    }

    var $rows = $host.children(".form-row").filter(function () {
      return !$(this).hasClass("field-DELETE") && !$(this).find("> .delete").length;
    });
    if ($rows.length) {
      $nest.insertAfter($rows.first());
      return;
    }
    $host.append($nest);
  }

  function ensureProfileFieldsVisible($related) {
    ["user_type", "profile_type", "is_active"].forEach(function (name) {
      var $row = findFormRow($related, name);
      if ($row && $row.length) $row.show();
    });
    $related.find('select[name$="-user_type"], select[name$="-profile_type"]').each(function () {
      var $row = $(this).closest(".form-row");
      if ($row.length) $row.show();
      $(this).show();
    });
    // Keep canonical order: user_type → profile_type → is_active → (nest) …
    var $ut = findFormRow($related, "user_type");
    var $pt = findFormRow($related, "profile_type");
    var $ia = findFormRow($related, "is_active");
    if ($ut.length && $pt.length) $pt.insertAfter($ut);
    if ($pt.length && $ia.length) $ia.insertAfter($pt);
    else if ($ut.length && $ia.length) $ia.insertAfter($ut);
  }

  function sourceTbody($dynGroup) {
    return $dynGroup.find("table tbody").first();
  }

  function isTemplateOrAddRow($tr) {
    if ($tr.hasClass("empty-form") || $tr.hasClass("add-row")) return true;
    var id = ($tr.attr("id") || "") + " " + ($tr.attr("class") || "");
    return id.indexOf("empty") >= 0;
  }

  function dynamicRows($dynGroup) {
    // Include rows currently parked under charge-profile nests.
    var $all = $().add($dynGroup.find("table tbody tr"));
    $(".cp-dynamic-fields-rows table tbody tr, .cp-dynamic-fields-orphan table tbody tr").each(function () {
      $all = $all.add(this);
    });
    return $all.filter(function () {
      var $tr = $(this);
      if (isTemplateOrAddRow($tr)) return false;
      return $tr.find("input, select, textarea").length > 0;
    });
  }

  function rowUserType($row) {
    var $ut = $row.find('input[name$="-user_type"], select[name$="-user_type"]').first();
    return ($ut.val() || "").toString();
  }

  function setRowUserType($row, userType) {
    var $ut = $row.find('input[name$="-user_type"], select[name$="-user_type"]').first();
    if ($ut.length) {
      $ut.val(userType);
      if ($ut.is("select") && !$ut.find('option[value="' + userType + '"]').length) {
        $ut.append($("<option>").attr("value", userType).text(userType));
        $ut.val(userType);
      }
    }
  }

  function nestHeaderHtml($dyn) {
    // Clone source tabular thead so labels stay in sync (Field key, Field label, …).
    var $srcThead = $dyn && $dyn.length ? $dyn.find("table thead").first() : $();
    if ($srcThead.length) {
      var $clone = $srcThead.clone(false);
      $clone.find("th").each(function () {
        var t = (($(this).text() || "") + " " + ($(this).find(".help").text() || "")).toLowerCase();
        // Keep column for alignment with hidden user_type cells, but do not show the label.
        if (t.indexOf("user type") >= 0 || t.indexOf("user_type") >= 0) {
          $(this).css({ display: "none", width: 0, padding: 0, border: 0 }).html("");
        }
      });
      return $("<div>").append($clone).html();
    }
    // Fallback labels if source thead is missing (user_type column kept empty/hidden)
    return (
      "<thead><tr>" +
      '<th style="display:none"></th>' +
      "<th>Field key</th>" +
      "<th>Field label</th>" +
      "<th>Field type</th>" +
      "<th>Is required</th>" +
      "<th>Editing required</th>" +
      "<th>Default value</th>" +
      "<th>Options</th>" +
      "<th>Help text</th>" +
      "<th>Source element field key</th>" +
      "<th>Delete?</th>" +
      "</tr></thead>"
    );
  }

  function nestRowsContainer($nest, $dyn) {
    var $wrap = $nest.find(".cp-dynamic-fields-rows").first();
    var $table = $wrap.children("table.cp-nested-dyn-table").first();
    if (!$table.length) {
      $wrap.empty();
      $table = $('<table class="tabular cp-nested-dyn-table" style="width:100%;"></table>');
      $wrap.append($table);
    }
    // Always refresh header from source so labels stay visible after regroup.
    $table.children("thead").remove();
    $table.prepend(nestHeaderHtml($dyn));
    var $tbody = $table.children("tbody").first();
    if (!$tbody.length) {
      $tbody = $("<tbody></tbody>");
      $table.append($tbody);
    }
    return $tbody;
  }

  function returnRowsToSource($charges, $dyn) {
    var $tbody = sourceTbody($dyn);
    if (!$tbody.length) return;
    $charges.find(".cp-dynamic-fields-rows tr, .cp-dynamic-fields-orphan tr").each(function () {
      if (!isTemplateOrAddRow($(this))) {
        $tbody.append(this);
      }
    });
  }

  function scheduleRegroup(delay) {
    if (regroupTimer) window.clearTimeout(regroupTimer);
    regroupTimer = window.setTimeout(function () {
      regroupTimer = null;
      regroup();
    }, delay || 40);
  }

  function regroup() {
    var $dyn = findDynamicGroup();
    var $charges = findChargeGroup();
    if (!$dyn.length || !$charges.length) return;

    // Soft-hide standalone dynamic section (keep management form / empty-form in DOM).
    $dyn.addClass("cp-dynamic-fields-source");
    $dyn.find("> h2, .inline-heading").first().hide();
    // Keep it in-flow but invisible so Django formset add still works reliably.
    $dyn.css({
      position: "absolute",
      left: "-10000px",
      top: "0",
      width: "1px",
      height: "1px",
      overflow: "hidden",
      opacity: "0",
      pointerEvents: "none",
      margin: "0",
      padding: "0",
      border: "none",
    });

    $dyn.find("thead th").each(function () {
      var t = ($(this).text() || "").toLowerCase();
      if (t.indexOf("user type") >= 0 || t.indexOf("user_type") >= 0) {
        $(this).hide();
      }
    });

    // Move nested rows back before rebuilding (avoids losing rows / duplicating nests).
    returnRowsToSource($charges, $dyn);

    var $relateds = $charges.find(".inline-related").not(".empty-form");
    $relateds.each(function () {
      var $rel = $(this);
      ensureProfileFieldsVisible($rel);
      ensureNest($rel);
    });

    var nestByUserType = {};
    $relateds.each(function () {
      var $rel = $(this);
      var $sel = $rel.find('select[name$="-user_type"]').first();
      var decoded = decodeUserType($sel.val());
      var $nest = ensureNest($rel);
      var $tbody = nestRowsContainer($nest, $dyn);
      var $note = $nest.find(".cp-dynamic-fields-pi-note");
      var $add = $nest.find(".cp-add-dynamic-field");
      var $headerHelp = $nest.find(".cp-dynamic-fields-header .help");

      ensureProfileFieldsVisible($rel);

      if (decoded.isPi) {
        var hasStandard = false;
        $relateds.each(function () {
          var other = decodeUserType($(this).find('select[name$="-user_type"]').first().val());
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
          $nest.find(".cp-dynamic-fields-rows").hide();
          if ($headerHelp.length) {
            $headerHelp.text(
              "PI IIT Faculty uses the same dynamic fields as IITR Faculty (no separate field set)."
            );
          }
          return;
        }
        $note
          .show()
          .text(
            "No standard charge profile for this user type yet. Fields added here apply to user type “" +
              (decoded.userType || "faculty") +
              "” (shared if you later add a standard row)."
          );
        $add.show();
        $nest.find(".cp-dynamic-fields-rows").show();
        if (decoded.userType) nestByUserType[decoded.userType] = $tbody;
        return;
      }

      $note.hide().text("");
      $add.show();
      $nest.find(".cp-dynamic-fields-rows").show();
      if ($headerHelp.length) {
        $headerHelp.text(
          "Fields A–Z for this user type (used in formulas and booking). User type is assumed from this charge profile."
        );
      }
      if (decoded.userType) nestByUserType[decoded.userType] = $tbody;
    });

    var $fallback = $charges.children(".cp-dynamic-fields-orphan").first();
    if (!$fallback.length) {
      $fallback = $(
        '<div class="cp-dynamic-fields-orphan" style="display:none;margin:12px 0;padding:12px;border:1px dashed #ba2121;background:#fff5f5;">' +
          "<strong>Unassigned dynamic fields</strong>" +
          '<p class="help">These rows have no matching charge profile user type yet. Add a charge profile for that user type.</p>' +
          '<div class="cp-dynamic-fields-rows"><table class="tabular cp-nested-dyn-table" style="width:100%;"><thead></thead><tbody></tbody></table></div>' +
        "</div>"
      );
      $charges.append($fallback);
    }
    var $fallbackBody = nestRowsContainer($fallback, $dyn);

    dynamicRows($dyn).each(function () {
      var $row = $(this);
      if (isTemplateOrAddRow($row)) return;
      var ut = rowUserType($row);
      $row.find(".field-user_type, td.field-user_type").hide();
      var $target = (ut && nestByUserType[ut]) || $fallbackBody;
      $target.append($row);
    });

    if ($fallbackBody.children("tr").length === 0) {
      $fallback.hide();
    } else {
      $fallback.show();
    }
  }

  function newestDynamicRow($dyn) {
    var $rows = dynamicRows($dyn);
    return $rows.last();
  }

  function addFieldForUserType(userType) {
    var $dyn = findDynamicGroup();
    if (!$dyn.length || !userType) return;

    var beforeCount = dynamicRows($dyn).length;
    var $add = $dyn.find(".add-row a").first();
    if (!$add.length) {
      $add = $dyn.find("tr.add-row a, .tabular .add-row a").first();
    }
    if (!$add.length) {
      window.alert("Could not find Django “add another” control for dynamic fields.");
      return;
    }

    // Temporarily allow interaction with the off-screen formset add link.
    $dyn.css({ pointerEvents: "auto" });
    $add.get(0).click();

    var tries = 0;
    function afterAdd() {
      tries += 1;
      var $rows = dynamicRows($dyn);
      if ($rows.length <= beforeCount && tries < 20) {
        window.setTimeout(afterAdd, 25);
        return;
      }
      var $last = newestDynamicRow($dyn);
      if ($last.length) setRowUserType($last, userType);
      $dyn.css({ pointerEvents: "none" });
      regroup();
    }
    window.setTimeout(afterAdd, 30);
  }

  $(document).ready(function () {
    var $charges = findChargeGroup();
    if (!$charges.length) return;

    regroup();

    $(document).on("change", '#charge_profiles-group select[name$="-user_type"], #charge_profiles-group select[name$="-profile_type"]', function () {
      scheduleRegroup(50);
    });

    $(document).on("click", ".cp-add-dynamic-field", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var $rel = $(this).closest(".inline-related");
      var raw = $rel.find('select[name$="-user_type"]').first().val();
      var decoded = decodeUserType(raw);
      if (!decoded.userType) return;
      if (decoded.isPi) {
        // Only block when a standard row already owns the fields.
        var hasStandard = false;
        findChargeGroup()
          .find(".inline-related")
          .not(".empty-form")
          .each(function () {
            var other = decodeUserType($(this).find('select[name$="-user_type"]').first().val());
            if (!other.isPi && other.userType === decoded.userType) hasStandard = true;
          });
        if (hasStandard) return;
      }
      addFieldForUserType(decoded.userType);
    });

    $(document).on("formset:added", function (event, $row) {
      // If a charge-profile row was added, build its nest; if a dynamic row, regroup.
      scheduleRegroup(60);
    });

    $(document).on("click", "#charge_profiles-group .add-row a", function () {
      scheduleRegroup(100);
    });
  });
})(django.jQuery);
