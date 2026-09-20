(function () {
  'use strict';

  function initTeacherOnboarding() {
    var rows = document.getElementById('quickRows');
    var addButton = document.getElementById('addQuickRow');
    var template = document.getElementById('quickRowTemplate');

    function updateRowMetadata() {
      if (!rows) return;
      rows.querySelectorAll('.quick-row').forEach(function (row, index) {
        var rowNumber = index + 1;
        var labels = [
          'الاسم الكامل للصف ',
          'رقم الجوال للصف ',
          'رقم الهوية للصف ',
          'المسمى الوظيفي للصف ',
          'القسم للصف ',
          'المختبر للصف '
        ];
        row.querySelectorAll('input, select').forEach(function (field, fieldIndex) {
          if (labels[fieldIndex]) field.setAttribute('aria-label', labels[fieldIndex] + rowNumber);
        });
        var removeButton = row.querySelector('.remove-quick-row');
        if (removeButton) {
          removeButton.value = String(index);
          removeButton.setAttribute('aria-label', 'حذف الصف ' + rowNumber);
        }
      });
    }

    function bindRemoveButtons(scope) {
      (scope || document).querySelectorAll('.remove-quick-row').forEach(function (button) {
        if (button.dataset.bound) return;
        button.dataset.bound = '1';
        button.addEventListener('click', function (event) {
          event.preventDefault();
          var row = button.closest('.quick-row');
          var allRows = rows ? rows.querySelectorAll('.quick-row') : [];
          if (row && allRows.length > 1) {
            row.remove();
          } else if (row) {
            row.querySelectorAll('input').forEach(function (input) { input.value = ''; });
          }
          updateRowMetadata();
        });
      });
    }

    if (rows && addButton && template) {
      bindRemoveButtons(rows);
      updateRowMetadata();
      addButton.addEventListener('click', function (event) {
        event.preventDefault();
        var fragment = template.content.cloneNode(true);
        rows.appendChild(fragment);
        bindRemoveButtons(rows);
        updateRowMetadata();
        var newRow = rows.lastElementChild;
        var firstInput = newRow ? newRow.querySelector('input') : null;
        if (firstInput) firstInput.focus();
      });
    }

    var dropZone = document.getElementById('onboardingDropZone');
    var fileInput = document.getElementById('onboardingFile');
    var fileName = document.getElementById('onboardingFileName');
    var previewButton = document.getElementById('filePreviewButton');
    var fileForm = document.getElementById('filePreviewForm');
    if (dropZone && fileInput) {
      function updateFile() {
        var file = fileInput.files && fileInput.files[0];
        if (fileName) fileName.textContent = file ? file.name : 'لم يتم اختيار ملف';
      }
      ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (eventName) {
        dropZone.addEventListener(eventName, function (event) {
          event.preventDefault();
          event.stopPropagation();
        });
      });
      ['dragenter', 'dragover'].forEach(function (eventName) {
        dropZone.addEventListener(eventName, function () { dropZone.classList.add('is-drag'); });
      });
      ['dragleave', 'drop'].forEach(function (eventName) {
        dropZone.addEventListener(eventName, function () { dropZone.classList.remove('is-drag'); });
      });
      dropZone.addEventListener('drop', function (event) {
        if (event.dataTransfer && event.dataTransfer.files.length) {
          fileInput.files = event.dataTransfer.files;
          updateFile();
        }
      });
      fileInput.addEventListener('change', updateFile);
      if (fileForm && previewButton) {
        fileForm.addEventListener('submit', function () {
          if (fileInput.files && fileInput.files.length) {
            previewButton.disabled = true;
            fileForm.setAttribute('aria-busy', 'true');
          }
        });
      }
      updateFile();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTeacherOnboarding);
  } else {
    initTeacherOnboarding();
  }
})();
