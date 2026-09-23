'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Control {
  constructor(kind, value = '') {
    this.kind = kind;
    this.value = value;
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.focused = false;
  }

  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  focus() {
    this.focused = true;
  }

  click() {
    let prevented = false;
    this.listeners.click({
      preventDefault() {
        prevented = true;
      },
    });
    return prevented;
  }
}

class Row {
  constructor(rows, name = '') {
    this.rows = rows;
    this.inputs = [
      new Control('input', name),
      new Control('input'),
      new Control('input'),
    ];
    this.selects = [new Control('select', 'teacher'), new Control('select'), new Control('select')];
    this.removeButton = new Control('button');
    this.removeButton.closest = () => this;
  }

  querySelectorAll(selector) {
    if (selector === 'input, select') return [...this.inputs, ...this.selects];
    if (selector === 'input') return this.inputs;
    return [];
  }

  querySelector(selector) {
    if (selector === '.remove-quick-row') return this.removeButton;
    if (selector === 'input') return this.inputs[0];
    return null;
  }

  remove() {
    const index = this.rows.children.indexOf(this);
    if (index >= 0) this.rows.children.splice(index, 1);
  }
}

class Rows {
  constructor() {
    this.children = [];
  }

  querySelectorAll(selector) {
    if (selector === '.quick-row') return this.children;
    if (selector === '.remove-quick-row') {
      return this.children.map((row) => row.removeButton);
    }
    return [];
  }

  appendChild(row) {
    this.children.push(row);
  }

  get lastElementChild() {
    return this.children[this.children.length - 1] || null;
  }
}

const rows = new Rows();
rows.children.push(new Row(rows, 'الأول'), new Row(rows, 'الثاني'), new Row(rows, 'الثالث'));

const addButton = new Control('button');
const template = {
  content: {
    cloneNode() {
      return new Row(rows);
    },
  },
};

const elements = {
  quickRows: rows,
  addQuickRow: addButton,
  quickRowTemplate: template,
  onboardingDropZone: null,
  onboardingFile: null,
  onboardingFileName: null,
  filePreviewButton: null,
};

const document = {
  readyState: 'complete',
  getElementById(id) {
    return elements[id] || null;
  },
  querySelectorAll() {
    return [];
  },
};

const sourcePath = path.resolve(__dirname, '../../../static/js/teacher-onboarding.js');
const source = fs.readFileSync(sourcePath, 'utf8');
vm.runInNewContext(source, { document });

assert.equal(rows.children.length, 3);
assert.ok(rows.children.every((row) => row.removeButton.dataset.bound === '1'));

assert.equal(addButton.click(), true);
assert.equal(rows.children.length, 4);
assert.equal(rows.children[3].inputs[0].focused, true);
assert.equal(rows.children[3].removeButton.value, '3');
assert.equal(rows.children[3].inputs[0].attributes['aria-label'], 'الاسم الكامل للصف 4');
assert.equal(rows.children[3].selects[2].attributes['aria-label'], 'المختبر للصف 4');

assert.equal(rows.children[1].removeButton.click(), true);
assert.deepEqual(
  rows.children.map((row) => row.inputs[0].value),
  ['الأول', 'الثالث', ''],
);
assert.deepEqual(
  rows.children.map((row) => row.removeButton.value),
  ['0', '1', '2'],
);
assert.equal(rows.children[1].removeButton.attributes['aria-label'], 'حذف الصف 2');

while (rows.children.length > 1) rows.children[1].removeButton.click();
rows.children[0].inputs[0].value = 'سيتم مسحه';
assert.equal(rows.children[0].removeButton.click(), true);
assert.equal(rows.children.length, 1);
assert.equal(rows.children[0].inputs[0].value, '');

console.log('teacher-onboarding.js DOM behavior: OK');
