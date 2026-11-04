import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../design_system.dart';
import '../models.dart';
import '../state.dart';
import '../widgets/status_widgets.dart';

const _labels = {
  '': 'الكل',
  'critical': 'حرج',
  'warning': 'تحذير',
  'info': 'معلومة',
};

class IncidentCenter extends ConsumerStatefulWidget {
  const IncidentCenter({super.key, this.selectedId});
  final int? selectedId;

  @override
  ConsumerState<IncidentCenter> createState() => _IncidentCenterState();
}

class _IncidentCenterState extends ConsumerState<IncidentCenter> {
  String _severity = '';
  int _offset = 0;

  @override
  Widget build(BuildContext context) {
    final query = (severity: _severity, offset: _offset);
    final page = ref.watch(incidentCasesProvider(query));
    final allowed =
        ref
            .watch(dashboardProvider)
            .asData
            ?.value
            .currentUser
            .can('acknowledge_incidents') ??
        false;
    final id = widget.selectedId;
    final selected = id == null
        ? null
        : ref.watch(incidentTimelineProvider((id: id, offset: 0)));
    final focused = selected?.asData?.value.incident;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Wrap(
          alignment: WrapAlignment.spaceBetween,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Text('مركز الحوادث', style: Theme.of(context).textTheme.titleLarge),
            TextButton.icon(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const _QuietHoursScreen()),
              ),
              icon: const Icon(Icons.bedtime_outlined),
              label: const Text('ساعات الهدوء'),
            ),
          ],
        ),
        if (selected != null)
          selected.when(
            data: (value) => Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'التنبيه المحدد',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                _IncidentCard(
                  incident: value.incident,
                  canAcknowledge: allowed,
                  highlighted: true,
                ),
              ],
            ),
            loading: () => const LinearProgressIndicator(),
            error: (error, _) => _IncidentLoadError(
              error: error,
              retry: () => ref.invalidate(
                incidentTimelineProvider((id: id!, offset: 0)),
              ),
            ),
          ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 4,
          children: [
            for (final filter in _labels.entries)
              ChoiceChip(
                label: Text(filter.value),
                selected: _severity == filter.key,
                onSelected: (_) => setState(() {
                  _severity = filter.key;
                  _offset = 0;
                }),
              ),
          ],
        ),
        const SizedBox(height: 12),
        page.when(
          loading: () => const LinearProgressIndicator(),
          error: (error, _) => _IncidentLoadError(
            error: error,
            retry: () => ref.invalidate(incidentCasesProvider(query)),
          ),
          data: (value) => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (value.cases.isEmpty)
                const Card(
                  child: EmptyState(
                    icon: Icons.notifications_none,
                    title: 'لا توجد حالات مطابقة',
                    message:
                        'ستظهر هنا الحالات المسجلة حسب درجة الخطورة المختارة.',
                  ),
                ),
              for (final item in value.cases.where(
                (item) => item.id != focused?.id,
              ))
                _IncidentCard(
                  key: ValueKey(item.id),
                  incident: item,
                  canAcknowledge: allowed,
                  highlighted: false,
                ),
              _IncidentPaging(
                offset: _offset,
                next: value.nextOffset,
                onPage: (offset) => setState(() => _offset = offset),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _IncidentPaging extends StatelessWidget {
  const _IncidentPaging({
    required this.offset,
    required this.next,
    required this.onPage,
  });
  final int offset;
  final int? next;
  final ValueChanged<int> onPage;

  @override
  Widget build(BuildContext context) => Wrap(
    alignment: WrapAlignment.spaceBetween,
    children: [
      if (offset > 0)
        TextButton(
          onPressed: () => onPage(offset >= 30 ? offset - 30 : 0),
          child: const Text('الأحدث'),
        ),
      if (next != null)
        TextButton(onPressed: () => onPage(next!), child: const Text('الأقدم')),
    ],
  );
}

class _IncidentLoadError extends StatelessWidget {
  const _IncidentLoadError({required this.error, required this.retry});
  final Object error;
  final VoidCallback retry;

  @override
  Widget build(BuildContext context) => Card(
    child: ListTile(
      title: Text(
        error is ApiException
            ? (error as ApiException).message
            : 'تعذر تحميل البيانات',
      ),
      trailing: TextButton(
        onPressed: retry,
        child: const Text('إعادة المحاولة'),
      ),
    ),
  );
}

class _IncidentTimelineScreen extends ConsumerStatefulWidget {
  const _IncidentTimelineScreen({required this.id});
  final int id;

  @override
  ConsumerState<_IncidentTimelineScreen> createState() =>
      _IncidentTimelineScreenState();
}

class _IncidentTimelineScreenState
    extends ConsumerState<_IncidentTimelineScreen> {
  int _offset = 0;

  @override
  Widget build(BuildContext context) {
    final query = (id: widget.id, offset: _offset);
    return Scaffold(
      appBar: AppBar(title: const Text('الخط الزمني للحالة')),
      body: ref
          .watch(incidentTimelineProvider(query))
          .when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (error, _) => _IncidentLoadError(
              error: error,
              retry: () => ref.invalidate(incidentTimelineProvider(query)),
            ),
            data: (value) => ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text(
                  value.incident.title,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                Text(
                  'رُصدت ${value.incident.occurrenceCount} مرة · الأحدث أولًا',
                ),
                const SizedBox(height: 12),
                for (final event in value.events)
                  Card(
                    child: ListTile(
                      leading: const Icon(Icons.history),
                      title: Text(
                        '${event.label} · ${_labels[event.severity] ?? event.severity}',
                      ),
                      subtitle: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (event.occurredAt != null)
                            Text(
                              DateFormat(
                                'dd/MM/yyyy HH:mm:ss',
                              ).format(event.occurredAt!.toLocal()),
                              textDirection: TextDirection.ltr,
                            ),
                          if (event.message.isNotEmpty) Text(event.message),
                        ],
                      ),
                    ),
                  ),
                if (value.events.isEmpty)
                  const Text('لا توجد أحداث مسجلة لهذه الحالة.'),
                _IncidentPaging(
                  offset: _offset,
                  next: value.nextOffset,
                  onPage: (offset) => setState(() => _offset = offset),
                ),
              ],
            ),
          ),
    );
  }
}

class _QuietHoursScreen extends ConsumerStatefulWidget {
  const _QuietHoursScreen();

  @override
  ConsumerState<_QuietHoursScreen> createState() => _QuietHoursScreenState();
}

class _QuietHoursScreenState extends ConsumerState<_QuietHoursScreen> {
  late Future<IncidentPreferences> _settings;
  bool? _enabled;
  TimeOfDay? _start, _end;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _settings = ref.read(apiProvider).incidentPreferences();
  }

  TimeOfDay _fromMinute(int minute) =>
      TimeOfDay(hour: minute ~/ 60, minute: minute % 60);
  int _minute(TimeOfDay value) => value.hour * 60 + value.minute;
  String _time(TimeOfDay value) =>
      '${value.hour.toString().padLeft(2, '0')}:${value.minute.toString().padLeft(2, '0')}';

  Future<void> _choose(bool isStart, TimeOfDay initial) async {
    final value = await showTimePicker(
      context: context,
      initialTime: initial,
      helpText: isStart ? 'بداية الهدوء' : 'نهاية الهدوء',
      cancelText: 'إلغاء',
      confirmText: 'اختيار',
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(alwaysUse24HourFormat: true),
        child: child!,
      ),
    );
    if (value == null || !mounted) return;
    setState(() {
      if (isStart) {
        _start = value;
      } else {
        _end = value;
      }
    });
  }

  Future<void> _save(bool enabled, TimeOfDay start, TimeOfDay end) async {
    if (_saving) return;
    if (enabled && start == end) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('اختر وقتين مختلفين.')));
      return;
    }
    setState(() => _saving = true);
    try {
      await ref
          .read(apiProvider)
          .saveIncidentPreferences(
            IncidentPreferences(
              enabled ? _minute(start) : null,
              enabled ? _minute(end) : null,
            ),
          );
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('تم حفظ ساعات الهدوء.')));
        Navigator.of(context).pop();
      }
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('$error')));
        setState(() => _saving = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('ساعات الهدوء')),
    body: FutureBuilder<IncidentPreferences>(
      future: _settings,
      builder: (context, snapshot) {
        if (snapshot.hasError) {
          return _IncidentLoadError(
            error: snapshot.error!,
            retry: () => setState(() {
              _settings = ref.read(apiProvider).incidentPreferences();
            }),
          );
        }
        final settings = snapshot.data;
        if (settings == null) {
          return const Center(child: CircularProgressIndicator());
        }
        final enabled = _enabled ?? settings.enabled;
        final start = _start ?? _fromMinute(settings.start ?? 1320);
        final end = _end ?? _fromMinute(settings.end ?? 420);
        return ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const Text(
              'يطبق الجدول على حسابك بتوقيت الرياض. تبقى المعلومات في سجل الحوادث، وتصل التنبيهات الحرجة والتحذيرات في وقتها.',
            ),
            const SizedBox(height: 12),
            SwitchListTile(
              title: const Text('تفعيل ساعات الهدوء'),
              value: enabled,
              onChanged: _saving
                  ? null
                  : (value) => setState(() => _enabled = value),
            ),
            if (enabled) ...[
              ListTile(
                title: const Text('البداية'),
                trailing: Text(_time(start)),
                onTap: _saving ? null : () => _choose(true, start),
              ),
              ListTile(
                title: const Text('النهاية'),
                trailing: Text(_time(end)),
                onTap: _saving ? null : () => _choose(false, end),
              ),
            ],
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _saving ? null : () => _save(enabled, start, end),
              child: Text(_saving ? 'جارٍ الحفظ…' : 'حفظ'),
            ),
          ],
        );
      },
    ),
  );
}

class _IncidentCard extends ConsumerStatefulWidget {
  const _IncidentCard({
    super.key,
    required this.incident,
    required this.canAcknowledge,
    required this.highlighted,
  });

  final IncidentInfo incident;
  final bool canAcknowledge;
  final bool highlighted;

  @override
  ConsumerState<_IncidentCard> createState() => _IncidentCardState();
}

class _IncidentCardState extends ConsumerState<_IncidentCard> {
  bool _submitting = false;

  Future<void> _acknowledge() async {
    if (_submitting) return;
    setState(() => _submitting = true);
    try {
      await ref
          .read(dashboardProvider.notifier)
          .acknowledge(widget.incident.id);
      if (mounted) {
        ref.invalidate(incidentCasesProvider);
        ref.invalidate(incidentTimelineProvider);
      }
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final item = widget.incident;
    final ops = context.ops;
    final (severity, color) = switch (item.severity) {
      'critical' => ('حرج', ops.danger),
      'warning' => ('تحذير', ops.gold),
      _ => ('معلومة', ops.info),
    };
    final status = switch (item.status) {
      'acknowledged' => 'تم الاطلاع',
      'resolved' => 'تم الحل',
      'open' => 'مفتوح',
      _ => 'حالة غير معروفة',
    };
    final date = item.resolvedAt ?? item.lastSeenAt ?? item.openedAt;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Card(
        shape: widget.highlighted
            ? RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(20),
                side: BorderSide(color: color, width: 2),
              )
            : null,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                '$severity · $status',
                style: TextStyle(color: color, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 4),
              Text(item.title, style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 4),
              if (item.status != 'resolved')
                Text(item.message, style: TextStyle(color: ops.slate)),
              Text('رُصدت ${item.occurrenceCount} مرة'),
              Align(
                alignment: AlignmentDirectional.centerStart,
                child: TextButton.icon(
                  icon: const Icon(Icons.timeline),
                  label: const Text('الخط الزمني'),
                  onPressed: () => Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => _IncidentTimelineScreen(id: item.id),
                    ),
                  ),
                ),
              ),
              if (item.projectName.isNotEmpty || date != null) ...[
                const SizedBox(height: 8),
                Wrap(
                  spacing: 12,
                  runSpacing: 4,
                  children: [
                    if (item.projectName.isNotEmpty) Text(item.projectName),
                    if (date != null)
                      Text(
                        DateFormat('dd/MM/yyyy HH:mm').format(date.toLocal()),
                        textDirection: TextDirection.ltr,
                      ),
                  ],
                ),
              ],
              if (widget.canAcknowledge && item.status == 'open')
                Align(
                  alignment: AlignmentDirectional.centerEnd,
                  child: TextButton.icon(
                    onPressed: _submitting ? null : _acknowledge,
                    icon: const Icon(Icons.done, size: 18),
                    label: Text(
                      _submitting ? 'جارٍ التسجيل…' : 'تسجيل الاطلاع',
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
