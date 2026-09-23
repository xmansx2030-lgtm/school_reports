import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat, NumberFormat;
import 'package:url_launcher/url_launcher.dart';

import '../api_client.dart';
import '../design_system.dart';
import '../models.dart';
import '../state.dart';
import '../widgets/status_widgets.dart';

class PaymentLinksScreen extends ConsumerStatefulWidget {
  const PaymentLinksScreen({super.key});

  @override
  ConsumerState<PaymentLinksScreen> createState() => _PaymentLinksScreenState();
}

class _PaymentLinksScreenState extends ConsumerState<PaymentLinksScreen> {
  int _projectId = 0;

  @override
  Widget build(BuildContext context) {
    final result = ref.watch(paymentLinksProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'روابط الدفع',
          style: TextStyle(fontWeight: FontWeight.w900),
        ),
        actions: [
          IconButton(
            tooltip: 'تحديث روابط الدفع',
            onPressed: () => ref.invalidate(paymentLinksProvider),
            icon: const Icon(Icons.refresh_rounded),
          ),
        ],
      ),
      body: result.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => _PaymentLinksError(
          message: error is ApiException
              ? error.message
              : 'تعذر تحميل روابط الدفع.',
          onRetry: () => ref.invalidate(paymentLinksProvider),
        ),
        data: _body,
      ),
    );
  }

  Widget _body(PaymentLinksData data) {
    final links = _projectId == 0
        ? data.links
        : data.links.where((link) => link.projectId == _projectId).toList();
    final pending = data.links.where((link) => link.isPayable).length;
    final paid = data.links.where((link) => link.status == 'paid').length;
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(paymentLinksProvider),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(16),
        children: [
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 900),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  PremiumPanel(
                    gradient: const LinearGradient(
                      begin: Alignment.topRight,
                      end: Alignment.bottomLeft,
                      colors: [OpsColors.ink, OpsColors.forest],
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Icon(
                          Icons.link_rounded,
                          color: OpsColors.gold,
                          size: 31,
                        ),
                        const SizedBox(height: 12),
                        const Text(
                          'تحصيل موحّد لجميع المشاريع',
                          style: TextStyle(
                            color: Colors.white,
                            fontSize: 20,
                            fontWeight: FontWeight.w900,
                          ),
                        ),
                        const SizedBox(height: 4),
                        const Text(
                          'أنشئ رابط ميّسر باسم المشروع، ثم شاركه مع العميل وتابع حالته من مكان واحد.',
                          style: TextStyle(color: OpsColors.onDarkSubtitle),
                        ),
                        const SizedBox(height: 16),
                        Wrap(
                          spacing: 10,
                          runSpacing: 10,
                          children: [
                            _HeroMetric(
                              label: 'بانتظار الدفع',
                              value: '$pending',
                            ),
                            _HeroMetric(label: 'مدفوعة', value: '$paid'),
                            _HeroMetric(
                              label: 'إجمالي السجلات',
                              value: '${data.links.length}',
                            ),
                          ],
                        ),
                      ],
                    ),
                  ),
                  if (!data.gatewayEnabled) ...[
                    const SizedBox(height: 14),
                    _GatewayDisabledNotice(),
                  ],
                  const SizedBox(height: 16),
                  LayoutBuilder(
                    builder: (context, constraints) {
                      final filter = DropdownButtonFormField<int>(
                        isExpanded: true,
                        initialValue: _projectId,
                        decoration: const InputDecoration(
                          labelText: 'تصفية حسب المشروع',
                          prefixIcon: Icon(Icons.apps_outlined),
                        ),
                        items: [
                          const DropdownMenuItem(
                            value: 0,
                            child: Text('جميع المشاريع'),
                          ),
                          ...data.projects.map(
                            (project) => DropdownMenuItem(
                              value: project.id,
                              child: Text(
                                project.name,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                          ),
                        ],
                        onChanged: (value) =>
                            setState(() => _projectId = value ?? 0),
                      );
                      final createButton = FilledButton.icon(
                        onPressed: data.projects.isEmpty
                            ? null
                            : () => _create(data.projects),
                        icon: const Icon(Icons.add_link_rounded),
                        label: const Text('رابط جديد'),
                        style: FilledButton.styleFrom(
                          minimumSize: const Size(132, 56),
                        ),
                      );
                      if (constraints.maxWidth < 560) {
                        return Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            filter,
                            if (data.canManage && data.gatewayEnabled) ...[
                              const SizedBox(height: 10),
                              createButton,
                            ],
                          ],
                        );
                      }
                      return Row(
                        children: [
                          Expanded(child: filter),
                          if (data.canManage && data.gatewayEnabled) ...[
                            const SizedBox(width: 10),
                            createButton,
                          ],
                        ],
                      );
                    },
                  ),
                  const SizedBox(height: 16),
                  if (links.isEmpty)
                    Card(
                      child: EmptyState(
                        icon: Icons.link_off_rounded,
                        title: _projectId == 0
                            ? 'لا توجد روابط دفع بعد'
                            : 'لا توجد روابط لهذا المشروع',
                        message: data.canManage && data.gatewayEnabled
                            ? 'أنشئ أول رابط بعد مراجعة بيانات العميل والمبلغ.'
                            : 'ستظهر الروابط هنا عند إنشائها من حساب مخوّل.',
                      ),
                    )
                  else
                    ...links.map(
                      (link) => Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: _PaymentLinkCard(
                          link: link,
                          canManage: data.canManage,
                          onChanged: () => ref.invalidate(paymentLinksProvider),
                        ),
                      ),
                    ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _create(List<PaymentProjectOption> projects) async {
    final created = await Navigator.of(context).push<PaymentLinkInfo>(
      MaterialPageRoute(
        builder: (_) => CreatePaymentLinkScreen(projects: projects),
      ),
    );
    if (created == null || !mounted) return;
    ref.invalidate(paymentLinksProvider);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('تم إنشاء رابط دفع ${created.projectName} بنجاح.'),
        backgroundColor: context.ops.forest,
      ),
    );
  }
}

class CreatePaymentLinkScreen extends ConsumerStatefulWidget {
  const CreatePaymentLinkScreen({required this.projects, super.key});

  final List<PaymentProjectOption> projects;

  @override
  ConsumerState<CreatePaymentLinkScreen> createState() =>
      _CreatePaymentLinkScreenState();
}

class _CreatePaymentLinkScreenState
    extends ConsumerState<CreatePaymentLinkScreen> {
  final _formKey = GlobalKey<FormState>();
  final _customerName = TextEditingController();
  final _customerPhone = TextEditingController();
  final _customerEmail = TextEditingController();
  final _amount = TextEditingController();
  final _description = TextEditingController();
  final _reference = TextEditingController();
  late int _projectId;
  int _expiryDays = 3;
  bool _submitting = false;

  @override
  void initState() {
    super.initState();
    _projectId = widget.projects.first.id;
  }

  @override
  void dispose() {
    _customerName.dispose();
    _customerPhone.dispose();
    _customerEmail.dispose();
    _amount.dispose();
    _description.dispose();
    _reference.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'إنشاء رابط دفع',
          style: TextStyle(fontWeight: FontWeight.w900),
        ),
      ),
      body: SafeArea(
        child: Form(
          key: _formKey,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 720),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      PremiumPanel(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            const SectionHeading(
                              icon: Icons.receipt_long_outlined,
                              title: 'بيانات التحصيل',
                              subtitle:
                                  'راجع المشروع والعميل والمبلغ قبل إنشاء الفاتورة لدى ميّسر.',
                            ),
                            const SizedBox(height: 18),
                            DropdownButtonFormField<int>(
                              isExpanded: true,
                              initialValue: _projectId,
                              decoration: const InputDecoration(
                                labelText: 'المشروع',
                                prefixIcon: Icon(Icons.apps_outlined),
                              ),
                              items: widget.projects
                                  .map(
                                    (project) => DropdownMenuItem(
                                      value: project.id,
                                      child: Text(
                                        project.name,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                      ),
                                    ),
                                  )
                                  .toList(),
                              onChanged: _submitting
                                  ? null
                                  : (value) => setState(
                                      () => _projectId = value ?? _projectId,
                                    ),
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _customerName,
                              textInputAction: TextInputAction.next,
                              decoration: const InputDecoration(
                                labelText: 'اسم العميل',
                                prefixIcon: Icon(Icons.person_outline_rounded),
                              ),
                              validator: _required('أدخل اسم العميل'),
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _customerPhone,
                              keyboardType: TextInputType.phone,
                              textDirection: TextDirection.ltr,
                              textInputAction: TextInputAction.next,
                              decoration: const InputDecoration(
                                labelText: 'جوال العميل مع مفتاح الدولة',
                                hintText: '+9665XXXXXXXX',
                                prefixIcon: Icon(Icons.phone_outlined),
                              ),
                              validator: (value) {
                                final digits = normalizeWhatsAppPhone(
                                  value ?? '',
                                );
                                return digits.length < 8 || digits.length > 15
                                    ? 'أدخل رقم جوال صالحًا مع مفتاح الدولة'
                                    : null;
                              },
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _customerEmail,
                              keyboardType: TextInputType.emailAddress,
                              textDirection: TextDirection.ltr,
                              textInputAction: TextInputAction.next,
                              decoration: const InputDecoration(
                                labelText: 'البريد الإلكتروني — اختياري',
                                prefixIcon: Icon(Icons.email_outlined),
                              ),
                              validator: (value) {
                                final email = value?.trim() ?? '';
                                if (email.isNotEmpty &&
                                    (!email.contains('@') ||
                                        !email.split('@').last.contains('.'))) {
                                  return 'أدخل بريدًا إلكترونيًا صالحًا';
                                }
                                return null;
                              },
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _amount,
                              keyboardType:
                                  const TextInputType.numberWithOptions(
                                    decimal: true,
                                  ),
                              textDirection: TextDirection.ltr,
                              textInputAction: TextInputAction.next,
                              inputFormatters: [
                                FilteringTextInputFormatter.allow(
                                  RegExp(r'^\d{0,9}([.]\d{0,2})?'),
                                ),
                              ],
                              decoration: const InputDecoration(
                                labelText: 'المبلغ بالريال',
                                prefixIcon: Icon(Icons.payments_outlined),
                                suffixText: 'SAR',
                              ),
                              validator: (value) {
                                final amount = double.tryParse(value ?? '');
                                return amount == null || amount < 1
                                    ? 'أدخل مبلغًا لا يقل عن ريال واحد'
                                    : null;
                              },
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _description,
                              minLines: 2,
                              maxLines: 4,
                              decoration: const InputDecoration(
                                labelText: 'تفاصيل الخدمة أو المنتج',
                                alignLabelWithHint: true,
                                prefixIcon: Icon(Icons.description_outlined),
                              ),
                              validator: _required('أدخل تفاصيل رابط الدفع'),
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _reference,
                              textDirection: TextDirection.ltr,
                              textInputAction: TextInputAction.next,
                              decoration: const InputDecoration(
                                labelText: 'مرجع داخلي — اختياري',
                                hintText: 'QUOTE-2026-001',
                                prefixIcon: Icon(Icons.tag_rounded),
                              ),
                            ),
                            const SizedBox(height: 12),
                            DropdownButtonFormField<int>(
                              isExpanded: true,
                              initialValue: _expiryDays,
                              decoration: const InputDecoration(
                                labelText: 'صلاحية الرابط',
                                prefixIcon: Icon(Icons.event_outlined),
                              ),
                              items: const [
                                DropdownMenuItem(
                                  value: 0,
                                  child: Text('بدون تاريخ انتهاء'),
                                ),
                                DropdownMenuItem(
                                  value: 1,
                                  child: Text('24 ساعة'),
                                ),
                                DropdownMenuItem(
                                  value: 3,
                                  child: Text('3 أيام'),
                                ),
                                DropdownMenuItem(
                                  value: 7,
                                  child: Text('7 أيام'),
                                ),
                                DropdownMenuItem(
                                  value: 14,
                                  child: Text('14 يومًا'),
                                ),
                                DropdownMenuItem(
                                  value: 30,
                                  child: Text('30 يومًا'),
                                ),
                              ],
                              onChanged: _submitting
                                  ? null
                                  : (value) => setState(
                                      () => _expiryDays = value ?? 3,
                                    ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 16),
                      FilledButton.icon(
                        onPressed: _submitting ? null : _reviewAndCreate,
                        icon: _submitting
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                            : const Icon(Icons.add_link_rounded),
                        label: Text(
                          _submitting
                              ? 'جاري الإنشاء...'
                              : 'مراجعة وإنشاء الرابط',
                        ),
                        style: FilledButton.styleFrom(
                          minimumSize: const Size.fromHeight(54),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  FormFieldValidator<String> _required(String message) =>
      (value) => value == null || value.trim().isEmpty ? message : null;

  Future<void> _reviewAndCreate() async {
    if (!_formKey.currentState!.validate()) return;
    final project = widget.projects.firstWhere((item) => item.id == _projectId);
    final amount = double.parse(_amount.text);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('تأكيد إنشاء رابط الدفع'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('المشروع: ${project.name}'),
            Text('العميل: ${_customerName.text.trim()}'),
            Text('المبلغ: ${amount.toStringAsFixed(2)} ريال'),
            const SizedBox(height: 10),
            const Text(
              'سيُنشأ سجل فعلي في حساب ميّسر. لن يُرسل الرابط للعميل تلقائيًا.',
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('رجوع'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('إنشاء الرابط'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _submitting = true);
    try {
      final created = await ref
          .read(apiProvider)
          .createPaymentLink(
            projectId: _projectId,
            customerName: _customerName.text.trim(),
            customerPhone: _customerPhone.text.trim(),
            customerEmail: _customerEmail.text.trim(),
            amount: amount,
            description: _description.text.trim(),
            internalReference: _reference.text.trim(),
            expiresAt: _expiryDays == 0
                ? null
                : DateTime.now().add(Duration(days: _expiryDays)),
          );
      if (mounted) Navigator.pop(context, created);
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error.message),
            backgroundColor: context.ops.danger,
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }
}

class _PaymentLinkCard extends ConsumerStatefulWidget {
  const _PaymentLinkCard({
    required this.link,
    required this.canManage,
    required this.onChanged,
  });

  final PaymentLinkInfo link;
  final bool canManage;
  final VoidCallback onChanged;

  @override
  ConsumerState<_PaymentLinkCard> createState() => _PaymentLinkCardState();
}

class _PaymentLinkCardState extends ConsumerState<_PaymentLinkCard> {
  bool _working = false;

  @override
  Widget build(BuildContext context) {
    final link = widget.link;
    final ops = context.ops;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: 44,
                  height: 44,
                  decoration: BoxDecoration(
                    color: ops.mint,
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Icon(Icons.receipt_long_outlined, color: ops.forest),
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        link.customerName,
                        style: TextStyle(
                          color: ops.ink,
                          fontSize: 16,
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                      Text(
                        link.projectName,
                        style: TextStyle(color: ops.slate, fontSize: 12),
                      ),
                    ],
                  ),
                ),
                _PaymentStatus(link: link),
              ],
            ),
            const SizedBox(height: 13),
            Text(
              link.description,
              style: TextStyle(color: ops.ink, height: 1.45),
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _FactChip(
                  icon: Icons.payments_outlined,
                  text:
                      '${NumberFormat('#,##0.00', 'ar').format(link.amount)} ${link.currency}',
                ),
                _FactChip(
                  icon: Icons.phone_outlined,
                  text: link.customerPhone,
                  ltr: true,
                ),
                if (link.internalReference.isNotEmpty)
                  _FactChip(
                    icon: Icons.tag_rounded,
                    text: link.internalReference,
                    ltr: true,
                  ),
                if (link.expiresAt != null)
                  _FactChip(
                    icon: Icons.event_outlined,
                    text: DateFormat('yyyy/MM/dd').format(link.expiresAt!),
                    ltr: true,
                  ),
              ],
            ),
            const SizedBox(height: 14),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (link.gatewayUrl.isNotEmpty)
                  FilledButton.icon(
                    onPressed: _working || !link.isPayable
                        ? null
                        : _openWhatsApp,
                    icon: const Icon(Icons.chat_outlined),
                    label: const Text('فتح واتساب'),
                  ),
                if (link.gatewayUrl.isNotEmpty)
                  OutlinedButton.icon(
                    onPressed: _working ? null : _copyLink,
                    icon: const Icon(Icons.copy_rounded),
                    label: const Text('نسخ الرابط'),
                  ),
                if (link.gatewayUrl.isNotEmpty)
                  OutlinedButton.icon(
                    onPressed: _working ? null : _openCheckout,
                    icon: const Icon(Icons.open_in_new_rounded),
                    label: const Text('معاينة'),
                  ),
                IconButton.filledTonal(
                  tooltip: 'مزامنة الحالة مع ميّسر',
                  onPressed: _working ? null : _sync,
                  icon: _working
                      ? const SizedBox(
                          width: 19,
                          height: 19,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.sync_rounded),
                ),
                if (widget.canManage && link.canCancel)
                  IconButton.outlined(
                    tooltip: 'إلغاء رابط الدفع',
                    color: ops.danger,
                    onPressed: _working ? null : _cancel,
                    icon: const Icon(Icons.block_rounded),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _copyLink() async {
    await Clipboard.setData(ClipboardData(text: widget.link.gatewayUrl));
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(const SnackBar(content: Text('تم نسخ رابط الدفع.')));
  }

  Future<void> _openCheckout() async {
    final opened = await launchUrl(
      Uri.parse(widget.link.gatewayUrl),
      mode: LaunchMode.externalApplication,
    );
    if (!opened && mounted) _showError('تعذر فتح صفحة الدفع.');
  }

  Future<void> _openWhatsApp() async {
    final opened = await launchUrl(
      paymentLinkWhatsAppUri(widget.link),
      mode: LaunchMode.externalApplication,
    );
    if (!opened && mounted) _showError('تعذر فتح واتساب على هذا الجهاز.');
  }

  Future<void> _sync() async {
    await _run(
      () => ref.read(apiProvider).syncPaymentLink(widget.link.publicId),
      success: 'تم تحديث حالة رابط الدفع من ميّسر.',
    );
  }

  Future<void> _cancel() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('إلغاء رابط الدفع؟'),
        content: Text(
          'سيُلغى رابط ${widget.link.customerName} لدى ميّسر، ولن يتمكن العميل من الدفع من خلاله بعد ذلك.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('رجوع'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: context.ops.danger),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('إلغاء الرابط'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    await _run(
      () => ref
          .read(apiProvider)
          .cancelPaymentLink(
            widget.link.publicId,
            confirmation: widget.link.cancellationConfirmation,
          ),
      success: 'تم إلغاء رابط الدفع لدى ميّسر.',
    );
  }

  Future<void> _run(
    Future<PaymentLinkInfo> Function() action, {
    required String success,
  }) async {
    setState(() => _working = true);
    try {
      await action();
      widget.onChanged();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(success), backgroundColor: context.ops.forest),
        );
      }
    } on ApiException catch (error) {
      if (mounted) _showError(error.message);
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  void _showError(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), backgroundColor: context.ops.danger),
    );
  }
}

class _PaymentStatus extends StatelessWidget {
  const _PaymentStatus({required this.link});

  final PaymentLinkInfo link;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    final (color, icon) = switch (link.status) {
      'paid' => (ops.healthy, Icons.check_circle_outline_rounded),
      'initiated' || 'on_hold' => (ops.warning, Icons.schedule_rounded),
      'failed' || 'canceled' || 'voided' => (ops.danger, Icons.cancel_outlined),
      'expired' => (ops.muted, Icons.timer_off_outlined),
      'refunded' => (ops.info, Icons.replay_rounded),
      _ => (ops.slate, Icons.hourglass_empty_rounded),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: color.withValues(alpha: .1),
        borderRadius: BorderRadius.circular(99),
        border: Border.all(color: color.withValues(alpha: .25)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 5),
          Text(
            link.statusLabel.isEmpty ? link.status : link.statusLabel,
            style: TextStyle(
              color: color,
              fontSize: 12,
              fontWeight: FontWeight.w800,
            ),
          ),
        ],
      ),
    );
  }
}

class _FactChip extends StatelessWidget {
  const _FactChip({required this.icon, required this.text, this.ltr = false});

  final IconData icon;
  final String text;
  final bool ltr;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    final maxWidth = (MediaQuery.sizeOf(context).width - 64)
        .clamp(180.0, 360.0)
        .toDouble();
    return Container(
      constraints: BoxConstraints(maxWidth: maxWidth),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: ops.surfaceAlt,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: ops.lineSoft),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 16, color: ops.slate),
          const SizedBox(width: 6),
          Flexible(
            child: Text(
              text,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textDirection: ltr ? TextDirection.ltr : null,
              style: TextStyle(color: ops.ink, fontWeight: FontWeight.w700),
            ),
          ),
        ],
      ),
    );
  }
}

class _HeroMetric extends StatelessWidget {
  const _HeroMetric({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
    decoration: BoxDecoration(
      color: Colors.white.withValues(alpha: .09),
      borderRadius: BorderRadius.circular(13),
      border: Border.all(color: Colors.white.withValues(alpha: .1)),
    ),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          value,
          style: const TextStyle(
            color: Colors.white,
            fontWeight: FontWeight.w900,
          ),
        ),
        const SizedBox(width: 7),
        Text(label, style: const TextStyle(color: OpsColors.onDarkSubtitle)),
      ],
    ),
  );
}

class _GatewayDisabledNotice extends StatelessWidget {
  @override
  Widget build(BuildContext context) => Card(
    color: context.ops.goldSoft,
    child: Padding(
      padding: const EdgeInsets.all(14),
      child: Row(
        children: [
          Icon(Icons.info_outline_rounded, color: context.ops.goldInk),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              'ميّسر غير مفعّل على الخادم؛ السجلات السابقة متاحة للقراءة لكن لا يمكن إنشاء رابط جديد.',
              style: TextStyle(color: context.ops.goldInk),
            ),
          ),
        ],
      ),
    ),
  );
}

class _PaymentLinksError extends StatelessWidget {
  const _PaymentLinksError({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          EmptyState(
            icon: Icons.cloud_off_outlined,
            title: 'تعذر تحميل روابط الدفع',
            message: message,
          ),
          FilledButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh_rounded),
            label: const Text('إعادة المحاولة'),
          ),
        ],
      ),
    ),
  );
}
