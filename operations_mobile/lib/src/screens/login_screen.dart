import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api_client.dart';
import '../design_system.dart';
import '../state.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key, this.notice});

  /// An optional message carried over from a previous session (e.g. expiry).
  final String? notice;

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _phone = TextEditingController();
  final _password = TextEditingController();
  final _otp = TextEditingController();
  bool _obscure = true;
  bool _needsOtp = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _phone.dispose();
    _password.dispose();
    _otp.dispose();
    super.dispose();
  }

  Future<String> _deviceName() async {
    try {
      final info = await DeviceInfoPlugin().androidInfo;
      final name = '${info.brand} ${info.model}'.trim();
      return name.isEmpty ? 'جهاز أندرويد' : name;
    } catch (_) {
      return 'جهاز أندرويد';
    }
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(sessionProvider.notifier)
          .signIn(
            phone: _phone.text.trim(),
            password: _password.text,
            otp: _otp.text.trim(),
            deviceName: await _deviceName(),
          );
      // On success the app shell swaps this screen out automatically.
    } on ApiException catch (error) {
      final needsOtp =
          error.statusCode == 401 &&
          (error.data?['otp_required'] == true ||
              error.message.contains('رمز'));
      setState(() {
        _busy = false;
        _error = error.message;
        if (needsOtp) _needsOtp = true;
      });
    } catch (_) {
      setState(() {
        _busy = false;
        _error = 'تعذر إكمال تسجيل الدخول الآن.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [OpsColors.ink, OpsColors.forest],
          ),
        ),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    _brand(),
                    const SizedBox(height: 26),
                    PremiumPanel(
                      padding: const EdgeInsets.all(22),
                      child: Form(
                        key: _formKey,
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            Text(
                              'تسجيل الدخول',
                              style: TextStyle(
                                fontSize: 20,
                                fontWeight: FontWeight.w900,
                                color: context.ops.ink,
                              ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'ادخل ببيانات حسابك في مركز العمليات',
                              style: TextStyle(
                                color: context.ops.slate,
                                fontSize: 13,
                              ),
                            ),
                            const SizedBox(height: 20),
                            if (_error != null) ...[
                              _errorBanner(_error!),
                              const SizedBox(height: 14),
                            ] else if (widget.notice != null) ...[
                              _noticeBanner(widget.notice!),
                              const SizedBox(height: 14),
                            ],
                            TextFormField(
                              controller: _phone,
                              keyboardType: TextInputType.phone,
                              textInputAction: TextInputAction.next,
                              inputFormatters: [
                                FilteringTextInputFormatter.allow(
                                  RegExp(r'[0-9+]'),
                                ),
                              ],
                              decoration: const InputDecoration(
                                labelText: 'رقم الجوال',
                                hintText: '05xxxxxxxx',
                                prefixIcon: Icon(Icons.phone_iphone_outlined),
                              ),
                              validator: (value) =>
                                  (value == null || value.trim().length < 6)
                                  ? 'أدخل رقم جوال صحيح'
                                  : null,
                            ),
                            const SizedBox(height: 14),
                            TextFormField(
                              controller: _password,
                              obscureText: _obscure,
                              textInputAction: _needsOtp
                                  ? TextInputAction.next
                                  : TextInputAction.done,
                              onFieldSubmitted: (_) {
                                if (!_needsOtp) _submit();
                              },
                              decoration: InputDecoration(
                                labelText: 'كلمة المرور',
                                prefixIcon: const Icon(Icons.lock_outline),
                                suffixIcon: IconButton(
                                  tooltip: _obscure ? 'إظهار' : 'إخفاء',
                                  onPressed: () =>
                                      setState(() => _obscure = !_obscure),
                                  icon: Icon(
                                    _obscure
                                        ? Icons.visibility_outlined
                                        : Icons.visibility_off_outlined,
                                  ),
                                ),
                              ),
                              validator: (value) =>
                                  (value == null || value.isEmpty)
                                  ? 'أدخل كلمة المرور'
                                  : null,
                            ),
                            if (_needsOtp) ...[
                              const SizedBox(height: 14),
                              TextFormField(
                                controller: _otp,
                                keyboardType: TextInputType.number,
                                textInputAction: TextInputAction.done,
                                onFieldSubmitted: (_) => _submit(),
                                inputFormatters: [
                                  FilteringTextInputFormatter.digitsOnly,
                                ],
                                decoration: const InputDecoration(
                                  labelText: 'رمز التحقق',
                                  hintText: 'الرمز المرسل إلى جوالك',
                                  prefixIcon: Icon(Icons.pin_outlined),
                                ),
                                validator: (value) => _needsOtp &&
                                        (value == null || value.trim().isEmpty)
                                    ? 'أدخل رمز التحقق'
                                    : null,
                              ),
                            ],
                            const SizedBox(height: 22),
                            FilledButton.icon(
                              onPressed: _busy ? null : _submit,
                              icon: _busy
                                  ? const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2.4,
                                        color: Colors.white,
                                      ),
                                    )
                                  : const Icon(Icons.login_rounded),
                              label: Text(_busy ? 'جاري الدخول…' : 'دخول'),
                            ),
                          ],
                        ),
                      ),
                    ),
                    const SizedBox(height: 18),
                    const Text(
                      'اتصال آمن ببيئة الإنتاج · tawtheeq-ksa.com',
                      style: TextStyle(color: Color(0xFFB9CAC2), fontSize: 11),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _brand() => Column(
    children: [
      Container(
        width: 76,
        height: 76,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: .12),
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: Colors.white.withValues(alpha: .18)),
        ),
        child: const Icon(
          Icons.monitor_heart_outlined,
          color: Colors.white,
          size: 40,
        ),
      ),
      const SizedBox(height: 14),
      const Text(
        'مركز العمليات',
        style: TextStyle(
          color: Colors.white,
          fontSize: 24,
          fontWeight: FontWeight.w900,
        ),
      ),
      const SizedBox(height: 4),
      const Text(
        'إدارة ومراقبة خوادم منصة توثيق',
        style: TextStyle(color: Color(0xFFC7D8D0), fontSize: 13),
      ),
    ],
  );

  Widget _errorBanner(String message) {
    final ops = context.ops;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: ops.dangerSoft,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: ops.danger.withValues(alpha: .4)),
      ),
      child: Row(
        children: [
          Icon(Icons.error_outline, color: ops.danger, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              style: TextStyle(color: ops.danger, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  Widget _noticeBanner(String message) {
    final ops = context.ops;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: ops.goldSoft,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: ops.gold.withValues(alpha: .4)),
      ),
      child: Row(
        children: [
          Icon(Icons.info_outline, color: ops.warning, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              style: TextStyle(color: ops.goldInk, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}
