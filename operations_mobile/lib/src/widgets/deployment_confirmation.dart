import 'package:flutter/material.dart';

import '../design_system.dart';
import '../models.dart';

/// Returns only the explicitly entered revision. Dismissal never authorizes a
/// deployment; the caller remains responsible for permissions and the API call.
Future<String?> confirmDeployment(BuildContext context, DeploymentInfo info) =>
    showDialog<String>(
      context: context,
      builder: (_) => DeploymentConfirmationDialog(info: info),
    );

class DeploymentConfirmationDialog extends StatefulWidget {
  const DeploymentConfirmationDialog({super.key, required this.info});

  final DeploymentInfo info;

  @override
  State<DeploymentConfirmationDialog> createState() =>
      _DeploymentConfirmationDialogState();
}

class _DeploymentConfirmationDialogState
    extends State<DeploymentConfirmationDialog> {
  final _confirmation = TextEditingController();
  bool _submitted = false;

  bool get _matches =>
      !_submitted &&
      widget.info.latestShortSha.isNotEmpty &&
      _confirmation.text == widget.info.latestShortSha;

  @override
  void dispose() {
    _confirmation.dispose();
    super.dispose();
  }

  void _confirm() {
    if (!_matches) return;
    _submitted = true;
    Navigator.of(context).pop(_confirmation.text);
  }

  @override
  Widget build(BuildContext context) {
    final info = widget.info;
    final revision = info.latestSha.isNotEmpty
        ? info.latestSha
        : info.latestShortSha;
    return AlertDialog(
      title: const Text('تأكيد النشر على الإنتاج'),
      scrollable: true,
      content: SizedBox(
        width: 460,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'سيُنشر الإصدار المحدد للمشروع على خادم الإنتاج. '
              'قد تُعاد تهيئة خدمات المشروع أثناء النشر.',
              style: TextStyle(color: context.ops.slate),
            ),
            const SizedBox(height: 16),
            _detail('المشروع', info.projectName),
            _detail('المستودع', info.repository, ltr: true),
            _detail('الفرع', info.branch, ltr: true),
            _detail('الإصدار المطلوب', revision, ltr: true),
            const SizedBox(height: 8),
            const Text('اكتب رمز الإصدار التالي حرفيًا لتأكيد النشر:'),
            const SizedBox(height: 6),
            SelectableText(
              info.latestShortSha,
              textDirection: TextDirection.ltr,
              textAlign: TextAlign.right,
              style: const TextStyle(fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 12),
            TextField(
              key: const ValueKey('deployment-confirmation-input'),
              controller: _confirmation,
              textDirection: TextDirection.ltr,
              autocorrect: false,
              enableSuggestions: false,
              textCapitalization: TextCapitalization.none,
              decoration: const InputDecoration(
                labelText: 'رمز الإصدار للتأكيد',
                border: OutlineInputBorder(),
              ),
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _confirm(),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          key: const ValueKey('cancel-deployment'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('إلغاء'),
        ),
        FilledButton.icon(
          key: const ValueKey('confirm-deployment'),
          onPressed: _matches ? _confirm : null,
          icon: const Icon(Icons.rocket_launch_outlined),
          label: const Text('تأكيد وبدء النشر'),
        ),
      ],
    );
  }

  Widget _detail(String label, String value, {bool ltr = false}) => Padding(
    padding: const EdgeInsets.only(bottom: 10),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(label, style: TextStyle(color: context.ops.slate, fontSize: 12)),
        const SizedBox(height: 3),
        SelectableText(
          value.isEmpty ? 'غير متاح' : value,
          textDirection: ltr ? TextDirection.ltr : TextDirection.rtl,
          textAlign: TextAlign.right,
          style: TextStyle(color: context.ops.ink, fontWeight: FontWeight.w700),
        ),
      ],
    ),
  );
}
