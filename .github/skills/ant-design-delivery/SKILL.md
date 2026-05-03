---
name: ant-design-delivery
description: >
  Ant Design (antd) delivery playbook. Use when building React UIs with Ant Design components,
  Form, Table, Layout, theme configuration, or Ant Design Pro patterns.
user-invocable: false
---

# Ant Design Delivery

## When To Apply

- React UIs using `antd` (Ant Design) component library.
- Admin dashboards, data tables, complex forms, modal workflows.
- Theme customisation via `ConfigProvider` or CSS variables.

## Component Usage Rules

- Import components from `antd` directly: `import { Button, Form, Table } from 'antd'`.
- Use `Form` with `Form.Item` and `name` props — never manage form state manually with `useState` for controlled inputs; let `Form` handle it.
- Use `Form.useForm()` hook; call `form.validateFields()` before submission.
- `Table` columns must define `key` or `dataIndex`; always provide a `rowKey` prop.
- Use `Space`, `Row`/`Col`, and `Flex` for layout; avoid raw flexbox CSS where antd utilities suffice.
- Modal and Drawer: control open state with a single boolean; use `destroyOnClose` when the content should reset.
- Use `message`, `notification`, and `Modal.confirm` for user feedback — not `alert()`.

## Theme & Styling

- Customise via `ConfigProvider` `theme={{ token: { ... } }}` — never override antd CSS classes directly.
- Use `useToken()` from `antd/es/theme` to access design tokens in custom components.
- Dark mode: wrap with `ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}`.

## Pro Components (if @ant-design/pro-components used)

- `ProTable` replaces `Table` for data-heavy admin views; pass `request` prop for async fetching.
- `ProForm` / `ProFormItem` provides built-in validation and layout; use it for complex forms.
- `PageContainer` provides breadcrumb, title, and content layout for admin pages.

## Quality Checklist

- All `Form.Item` fields have `name` and `rules` defined; required fields show clear error messages.
- Tables handle empty, loading, and error states (use `loading` prop and empty `locale`).
- No duplicate `key` warnings in lists or table rows.
- Theme tokens are applied consistently; no hard-coded colours that bypass the design system.
- Responsive: antd `Row`/`Col` with `xs`/`sm`/`md` breakpoints used for mobile layouts.
