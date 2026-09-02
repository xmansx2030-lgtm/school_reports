import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'design_system.dart';
import 'screens/dashboard_screen.dart';
import 'screens/login_screen.dart';
import 'state.dart';

class OperationsApp extends ConsumerWidget {
  const OperationsApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.listen<SessionState>(sessionProvider, (previous, next) {
      if (next.status == SessionStatus.signedIn &&
          previous?.status != SessionStatus.signedIn) {
        ref.read(notificationProvider).initialize();
      }
    });
    final session = ref.watch(sessionProvider);
    final themeMode = ref.watch(themeModeProvider);
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'مركز العمليات',
      locale: const Locale('ar'),
      builder: (context, child) =>
          Directionality(textDirection: TextDirection.rtl, child: child!),
      themeMode: themeMode,
      theme: _buildTheme(OpsPalette.light, Brightness.light),
      darkTheme: _buildTheme(OpsPalette.dark, Brightness.dark),
      home: switch (session.status) {
        SessionStatus.loading => const _StartupScreen(),
        SessionStatus.signedOut => LoginScreen(notice: session.error),
        SessionStatus.signedIn => const DashboardScreen(),
      },
    );
  }

  ThemeData _buildTheme(OpsPalette ops, Brightness brightness) {
    final scheme =
        ColorScheme.fromSeed(
          seedColor: OpsColors.forest,
          brightness: brightness,
        ).copyWith(
          primary: ops.forest,
          secondary: ops.gold,
          surface: ops.surface,
          error: ops.danger,
        );
    final base = ThemeData(brightness: brightness);
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: ops.canvas,
      fontFamily: 'Tajawal',
      extensions: [ops],
      textTheme: base.textTheme.apply(
        bodyColor: ops.ink,
        displayColor: ops.ink,
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: OpsColors.ink,
        foregroundColor: Colors.white,
        elevation: 0,
        surfaceTintColor: Colors.transparent,
        centerTitle: false,
      ),
      cardTheme: CardThemeData(
        color: ops.surface,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: const BorderRadius.all(Radius.circular(20)),
          side: BorderSide(color: ops.line),
        ),
      ),
      dividerTheme: DividerThemeData(color: ops.line),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: ops.surfaceAlt,
        border: OutlineInputBorder(
          borderRadius: const BorderRadius.all(Radius.circular(16)),
          borderSide: BorderSide(color: ops.line),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: const BorderRadius.all(Radius.circular(16)),
          borderSide: BorderSide(color: ops.line),
        ),
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: ops.forest,
          foregroundColor: Colors.white,
          minimumSize: const Size(48, 54),
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
          ),
          textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: ops.forest,
          foregroundColor: Colors.white,
          minimumSize: const Size(48, 54),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
          ),
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: ops.surface,
        indicatorColor: ops.mint,
        elevation: 3,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: brightness == Brightness.dark
            ? ops.surfaceAlt
            : OpsColors.ink,
        contentTextStyle: TextStyle(color: ops.ink),
      ),
      tooltipTheme: const TooltipThemeData(
        waitDuration: Duration(milliseconds: 450),
      ),
    );
  }
}

class _StartupScreen extends StatelessWidget {
  const _StartupScreen();
  @override
  Widget build(BuildContext context) => const Scaffold(
    body: Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.monitor_heart_outlined,
            size: 54,
            color: Color(0xFF006C35),
          ),
          SizedBox(height: 20),
          CircularProgressIndicator(),
        ],
      ),
    ),
  );
}
