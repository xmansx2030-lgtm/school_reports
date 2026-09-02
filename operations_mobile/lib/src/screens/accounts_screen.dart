import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api_client.dart';
import '../design_system.dart';
import '../models.dart';
import '../state.dart';

class AccountsScreen extends ConsumerWidget {
  const AccountsScreen({
    super.key,
    this.embedded = false,
    this.canManage = true,
  });

  final bool embedded;
  final bool canManage;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (!canManage) {
      return const _TeamAccessDenied();
    }
    final accounts = ref.watch(accountsProvider);
    final content = accounts.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (error, _) => _AccountError(
        message: error is ApiException
            ? error.message
            : 'تعذر تحميل فريق العمليات.',
        onRetry: () => ref.invalidate(accountsProvider),
      ),
      data: (items) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(accountsProvider),
        child: ListView.separated(
          padding: const EdgeInsets.all(16),
          itemCount: items.length + 1,
          separatorBuilder: (_, _) => const SizedBox(height: 10),
          itemBuilder: (context, index) {
            if (index == 0) {
              return Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text(
                    'فريق العمليات',
                    style: TextStyle(fontSize: 19, fontWeight: FontWeight.w900),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    'صلاحيات مستقلة عن حسابات المشروع الرئيسي.',
                    style: TextStyle(color: context.ops.slate),
                  ),
                  const SizedBox(height: 14),
                  FilledButton.icon(
                    onPressed: () => _showAccountForm(context, ref),
                    icon: const Icon(Icons.person_add_alt_1),
                    label: const Text('إضافة عضو'),
                  ),
                ],
              );
            }
            return _AccountTile(account: items[index - 1]);
          },
        ),
      ),
    );
    if (embedded) return content;
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'إدارة الحسابات',
          style: TextStyle(fontWeight: FontWeight.w900),
        ),
        actions: [
          IconButton(
            tooltip: 'إضافة حساب',
            onPressed: () => _showAccountForm(context, ref),
            icon: const Icon(Icons.person_add_alt_1),
          ),
          IconButton(
            tooltip: 'تحديث',
            onPressed: () => ref.invalidate(accountsProvider),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: content,
    );
  }

  Future<void> _showAccountForm(
    BuildContext context,
    WidgetRef ref, {
    OperationsAccount? account,
  }) async {
    final name = TextEditingController(text: account?.name ?? '');
    final phone = TextEditingController(text: account?.phone ?? '');
    final email = TextEditingController(text: account?.email ?? '');
    final password = TextEditingController();
    final formKey = GlobalKey<FormState>();
    var role = account?.role ?? 'viewer';
    var submitting = false;
    final created = account == null;
    final saved = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setState) => AlertDialog(
          title: Text(created ? 'إضافة حساب' : 'تعديل حساب'),
          content: Form(
            key: formKey,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextFormField(
                    controller: name,
                    decoration: const InputDecoration(labelText: 'الاسم'),
                    validator: (value) => value == null || value.trim().isEmpty
                        ? 'أدخل الاسم'
                        : null,
                  ),
                  const SizedBox(height: 10),
                  if (account?.role != 'owner')
                    DropdownButtonFormField<String>(
                      initialValue: role,
                      decoration: const InputDecoration(labelText: 'الدور'),
                      items: const [
                        DropdownMenuItem(
                          value: 'admin',
                          child: Text('مدير عمليات'),
                        ),
                        DropdownMenuItem(
                          value: 'operator',
                          child: Text('مشغّل'),
                        ),
                        DropdownMenuItem(
                          value: 'viewer',
                          child: Text('مشاهدة فقط'),
                        ),
                      ],
                      onChanged: submitting
                          ? null
                          : (value) => role = value ?? role,
                    ),
                  const SizedBox(height: 10),
                  TextFormField(
                    controller: phone,
                    enabled: created,
                    keyboardType: TextInputType.phone,
                    textDirection: TextDirection.ltr,
                    decoration: const InputDecoration(labelText: 'رقم الجوال'),
                    validator: (value) => value == null || value.trim().isEmpty
                        ? 'أدخل رقم الجوال'
                        : null,
                  ),
                  const SizedBox(height: 10),
                  TextFormField(
                    controller: email,
                    keyboardType: TextInputType.emailAddress,
                    textDirection: TextDirection.ltr,
                    decoration: const InputDecoration(
                      labelText: 'البريد الإلكتروني',
                    ),
                  ),
                  const SizedBox(height: 10),
                  TextFormField(
                    controller: password,
                    obscureText: true,
                    textDirection: TextDirection.ltr,
                    decoration: InputDecoration(
                      labelText: created
                          ? 'كلمة المرور'
                          : 'كلمة مرور جديدة اختياريًا',
                    ),
                    validator: (value) {
                      final text = value ?? '';
                      if (created && text.length < 10) {
                        return 'أدخل كلمة مرور من 10 أحرف على الأقل';
                      }
                      if (!created && text.isNotEmpty && text.length < 10) {
                        return 'كلمة المرور قصيرة';
                      }
                      return null;
                    },
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: submitting
                  ? null
                  : () => Navigator.pop(dialogContext, false),
              child: const Text('إلغاء'),
            ),
            ElevatedButton.icon(
              onPressed: submitting
                  ? null
                  : () async {
                      if (!formKey.currentState!.validate()) return;
                      setState(() => submitting = true);
                      try {
                        final api = ref.read(apiProvider);
                        if (created) {
                          await api.createAccount(
                            name: name.text.trim(),
                            phone: phone.text.trim(),
                            email: email.text.trim(),
                            password: password.text,
                            role: role,
                          );
                        } else {
                          await api.updateAccount(
                            account.id,
                            name: name.text.trim(),
                            email: email.text.trim(),
                            password: password.text.isEmpty
                                ? null
                                : password.text,
                            role: account.role == 'owner' ? null : role,
                          );
                        }
                        if (dialogContext.mounted) {
                          Navigator.pop(dialogContext, true);
                        }
                      } on ApiException catch (error) {
                        if (dialogContext.mounted) {
                          ScaffoldMessenger.of(dialogContext).showSnackBar(
                            SnackBar(content: Text(error.message)),
                          );
                        }
                        setState(() => submitting = false);
                      }
                    },
              icon: submitting
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.save_outlined),
              label: const Text('حفظ'),
            ),
          ],
        ),
      ),
    );
    name.dispose();
    phone.dispose();
    email.dispose();
    password.dispose();
    if (saved == true) {
      ref.invalidate(accountsProvider);
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('تم حفظ الحساب.')));
      }
    }
  }
}

class _AccountTile extends ConsumerWidget {
  const _AccountTile({required this.account});
  final OperationsAccount account;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Card(
    child: ListTile(
      leading: Icon(
        account.isActive ? Icons.verified_user_outlined : Icons.block,
        color: account.isActive ? context.ops.healthy : context.ops.danger,
      ),
      title: Text(
        account.name,
        style: const TextStyle(fontWeight: FontWeight.w900),
      ),
      subtitle: Text(
        [
          account.phone,
          account.roleLabel,
          account.isActive ? 'نشط' : 'معطل',
          if (account.activeDevices > 0) '${account.activeDevices} جهاز',
        ].join(' · '),
      ),
      trailing: account.role == 'owner'
          ? Icon(
              Icons.workspace_premium_outlined,
              color: context.ops.gold,
            )
          : PopupMenuButton<String>(
              tooltip: 'إجراءات الحساب',
              onSelected: (value) async {
                if (value == 'edit') {
                  await const AccountsScreen()._showAccountForm(
                    context,
                    ref,
                    account: account,
                  );
                } else if (value == 'toggle') {
                  await _toggle(context, ref);
                }
              },
              itemBuilder: (_) => [
                const PopupMenuItem(
                  value: 'edit',
                  child: ListTile(
                    leading: Icon(Icons.edit_outlined),
                    title: Text('تعديل'),
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
                PopupMenuItem(
                  value: 'toggle',
                  child: ListTile(
                    leading: Icon(account.isActive ? Icons.block : Icons.check),
                    title: Text(account.isActive ? 'تعطيل' : 'تفعيل'),
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
              ],
            ),
    ),
  );

  Future<void> _toggle(BuildContext context, WidgetRef ref) async {
    try {
      await ref
          .read(apiProvider)
          .updateAccount(account.id, isActive: !account.isActive);
      ref.invalidate(accountsProvider);
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('تم تحديث حالة الحساب.')));
      }
    } on ApiException catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }
}

class _TeamAccessDenied extends StatelessWidget {
  const _TeamAccessDenied();

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.lock_outline, size: 52, color: ops.muted),
            const SizedBox(height: 14),
            Text(
              'إدارة الفريق مخصصة لمدير العمليات',
              style: TextStyle(
                fontSize: 17,
                fontWeight: FontWeight.w900,
                color: ops.ink,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 6),
            Text(
              'يمكنك متابعة الخوادم حسب الصلاحيات الممنوحة لحسابك.',
              style: TextStyle(color: ops.slate),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

class _AccountError extends StatelessWidget {
  const _AccountError({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.manage_accounts_outlined,
            size: 54,
            color: context.ops.danger,
          ),
          const SizedBox(height: 14),
          Text(message, textAlign: TextAlign.center),
          const SizedBox(height: 18),
          ElevatedButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh),
            label: const Text('إعادة المحاولة'),
          ),
        ],
      ),
    ),
  );
}
