#!/usr/bin/env python3
"""Apply Nexa's client-side rewarded-connect changes to a pinned v2rayNG checkout."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise RuntimeError("Required source missing: " + rel)
    return path.read_text(encoding="utf-8")


def write(root: Path, rel: str, value: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def replace_once(value: str, old: str, new: str, label: str) -> str:
    count = value.count(old)
    if count != 1:
        if count == 0 and new in value:
            return value
        raise RuntimeError(label + ": expected one match, found " + str(count))
    return value.replace(old, new, 1)


def replace_function(source: str, signature: str, replacement: str, label: str) -> str:
    lines = source.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if signature in line and not line.lstrip().startswith("#")]
    if not indexes:
        if replacement.strip() in source:
            return source
        raise RuntimeError(label + ": method not found")
    if len(indexes) != 1:
        raise RuntimeError(label + ": method appears more than once")

    start = indexes[0]
    balance = 0
    opened = False
    end = None
    for i in range(start, len(lines)):
        # Target methods do not contain brace characters in string literals.
        clean = lines[i].split("//", 1)[0]
        opens = clean.count("{")
        closes = clean.count("}")
        if opens:
            opened = True
        balance += opens - closes
        if opened and balance == 0:
            end = i + 1
            break
    if end is None:
        raise RuntimeError(label + ": unmatched method braces")
    new_lines = replacement.rstrip() + "\n"
    return "".join(lines[:start]) + new_lines + "".join(lines[end:])


def add_import(source: str, package_line: str, additions: str, label: str) -> str:
    if additions.strip().splitlines()[0] in source:
        return source
    return replace_once(source, package_line, package_line + additions, label)


def patch_credit_store(root: Path) -> None:
    write(root, "V2rayNG/app/src/main/java/com/v2ray/ang/billing/ConnectionCreditStore.kt", r'''package com.v2ray.ang.billing

import android.content.ContentValues
import android.content.Context
import android.content.ContextWrapper
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.io.File
import java.util.UUID

/**
 * Local-only accounting for rewarded connection credits.
 *
 * SQLite transactions serialize writers across the UI and :daemon processes.
 * This protects against ordinary races and duplicate SDK callbacks, but is NOT
 * tamper-proof against root access, APK modification, or database replacement.
 */
object ConnectionCreditStore {
    private const val DB_NAME = "nexa_connection_credits.db"
    private const val DB_VERSION = 1

    private val helperLock = Any()
    @Volatile private var dbHelper: LedgerDb? = null

    private class NoBackupContext(base: Context) : ContextWrapper(base) {
        override fun getDatabasePath(name: String): File {
            return File(noBackupFilesDir, name).also { it.parentFile?.mkdirs() }
        }
    }

    private class LedgerDb(context: Context) :
        SQLiteOpenHelper(NoBackupContext(context.applicationContext), DB_NAME, null, DB_VERSION) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL("CREATE TABLE account (_id INTEGER PRIMARY KEY CHECK(_id = 1), credits INTEGER NOT NULL)")
            db.execSQL("INSERT INTO account (_id, credits) VALUES (1, 0)")
            db.execSQL("CREATE TABLE rewarded_ads (ad_id TEXT PRIMARY KEY NOT NULL, rewarded_at INTEGER NOT NULL)")
            db.execSQL(
                "CREATE TABLE connection_sessions (" +
                    "session_id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL)"
            )
            db.execSQL("CREATE TABLE active_session (_id INTEGER PRIMARY KEY CHECK(_id = 1), session_id TEXT)")
            db.execSQL("INSERT INTO active_session (_id, session_id) VALUES (1, NULL)")
        }

        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
            if (oldVersion != newVersion) {
                throw IllegalStateException("Unsupported credit ledger migration")
            }
        }
    }

    private fun helper(context: Context): LedgerDb {
        dbHelper?.let { return it }
        synchronized(helperLock) {
            dbHelper?.let { return it }
            return LedgerDb(context.applicationContext).also { dbHelper = it }
        }
    }

    private inline fun <T> transaction(context: Context, block: (SQLiteDatabase) -> T): T {
        val db = helper(context).writableDatabase
        db.beginTransaction()
        try {
            val result = block(db)
            db.setTransactionSuccessful()
            return result
        } finally {
            db.endTransaction()
        }
    }

    private fun scalarInt(db: SQLiteDatabase, sql: String, args: Array<String>? = null): Int {
        db.rawQuery(sql, args).use { cursor ->
            return if (cursor.moveToFirst()) cursor.getInt(0) else 0
        }
    }

    private fun status(db: SQLiteDatabase, sessionId: String): String? {
        db.rawQuery(
            "SELECT status FROM connection_sessions WHERE session_id = ?",
            arrayOf(sessionId)
        ).use { cursor ->
            return if (cursor.moveToFirst()) cursor.getString(0) else null
        }
    }

    private fun activeId(db: SQLiteDatabase): String? {
        db.rawQuery(
            "SELECT s.session_id FROM active_session a " +
                "JOIN connection_sessions s ON s.session_id = a.session_id " +
                "WHERE a._id = 1 AND s.status = 'ACTIVE'",
            null
        ).use { cursor ->
            return if (cursor.moveToFirst()) cursor.getString(0) else null
        }
    }

    fun balance(context: Context): Int {
        return scalarInt(helper(context).readableDatabase, "SELECT credits FROM account WHERE _id = 1")
    }

    /** Grants exactly three credits once per SDK ad-response ID. */
    fun grantReward(context: Context, adId: String): Boolean {
        if (adId.isBlank()) return false
        return transaction(context) { db ->
            val values = ContentValues().apply {
                put("ad_id", adId)
                put("rewarded_at", System.currentTimeMillis())
            }
            val rowId = db.insertWithOnConflict(
                "rewarded_ads", null, values, SQLiteDatabase.CONFLICT_IGNORE
            )
            if (rowId == -1L) {
                false
            } else {
                db.execSQL("UPDATE account SET credits = credits + 3 WHERE _id = 1")
                true
            }
        }
    }

    /**
     * Reserves one credit atomically. One pending or active session at a time
     * reflects Android's single-tunnel model.
     */
    fun reserveConnection(context: Context): String? {
        return transaction(context) { db ->
            if (activeId(db) != null) return@transaction null

            val pending = scalarInt(
                db,
                "SELECT COUNT(*) FROM connection_sessions WHERE status IN ('RESERVED','STARTING')"
            )
            if (pending != 0) return@transaction null

            val credits = scalarInt(db, "SELECT credits FROM account WHERE _id = 1")
            if (credits <= 0) return@transaction null

            val changed = db.update(
                "account",
                ContentValues().apply { put("credits", credits - 1) },
                "_id = 1 AND credits = ?",
                arrayOf(credits.toString())
            )
            if (changed != 1) return@transaction null

            val id = UUID.randomUUID().toString()
            db.insertOrThrow(
                "connection_sessions",
                null,
                ContentValues().apply {
                    put("session_id", id)
                    put("status", "RESERVED")
                    put("created_at", System.currentTimeMillis())
                }
            )
            id
        }
    }

    /**
     * A null/system intent may resume only an already ACTIVE session.
     * A new connection always needs the explicit ID created by reserveConnection.
     */
    fun claimStart(context: Context, requestedId: String?): String? {
        return transaction(context) { db ->
            if (requestedId.isNullOrBlank()) return@transaction activeId(db)

            when (status(db, requestedId)) {
                "RESERVED" -> {
                    val changed = db.update(
                        "connection_sessions",
                        ContentValues().apply { put("status", "STARTING") },
                        "session_id = ? AND status = 'RESERVED'",
                        arrayOf(requestedId)
                    )
                    if (changed == 1) requestedId else null
                }
                "STARTING", "ACTIVE" -> requestedId
                else -> null
            }
        }
    }

    fun isAuthorizedForCore(context: Context, sessionId: String?): Boolean {
        if (sessionId.isNullOrBlank()) return false
        return status(helper(context).readableDatabase, sessionId) in setOf("STARTING", "ACTIVE")
    }

    fun markActive(context: Context, sessionId: String): Boolean {
        return transaction(context) { db ->
            val changed = db.update(
                "connection_sessions",
                ContentValues().apply { put("status", "ACTIVE") },
                "session_id = ? AND status IN ('STARTING','ACTIVE')",
                arrayOf(sessionId)
            )
            if (changed != 1) {
                false
            } else {
                db.update(
                    "active_session",
                    ContentValues().apply { put("session_id", sessionId) },
                    "_id = 1",
                    null
                )
                true
            }
        }
    }

    /**
     * Failed RESERVED/STARTING attempts are refunded.
     * ACTIVE sessions are closed without refund because their tunnel was started.
     */
    fun finishSession(context: Context, sessionId: String?) {
        if (sessionId.isNullOrBlank()) return
        transaction(context) { db ->
            when (status(db, sessionId)) {
                "RESERVED", "STARTING" -> {
                    db.execSQL("UPDATE account SET credits = credits + 1 WHERE _id = 1")
                    db.update(
                        "connection_sessions",
                        ContentValues().apply { put("status", "FAILED") },
                        "session_id = ?",
                        arrayOf(sessionId)
                    )
                }
                "ACTIVE" -> {
                    db.update(
                        "connection_sessions",
                        ContentValues().apply { put("status", "STOPPED") },
                        "session_id = ?",
                        arrayOf(sessionId)
                    )
                }
            }
            db.update(
                "active_session",
                ContentValues().apply { putNull("session_id") },
                "_id = 1 AND session_id = ?",
                arrayOf(sessionId)
            )
            Unit
        }
    }

    fun currentActiveSessionId(context: Context): String? {
        return activeId(helper(context).readableDatabase)
    }

    /**
     * On an explicit app launch, return abandoned reservations only when there
     * is no active tunnel. A successful active session is never refunded.
     */
    fun recoverInterruptedSessions(context: Context) {
        transaction(context) { db ->
            if (activeId(db) != null) return@transaction Unit
            db.rawQuery(
                "SELECT session_id FROM connection_sessions WHERE status IN ('RESERVED','STARTING')",
                null
            ).use { cursor ->
                val ids = mutableListOf<String>()
                while (cursor.moveToNext()) ids += cursor.getString(0)
                ids.forEach { id ->
                    val changed = db.update(
                        "connection_sessions",
                        ContentValues().apply { put("status", "FAILED") },
                        "session_id = ? AND status IN ('RESERVED','STARTING')",
                        arrayOf(id)
                    )
                    if (changed == 1) {
                        db.execSQL("UPDATE account SET credits = credits + 1 WHERE _id = 1")
                    }
                }
            }
            Unit
        }
    }

    internal fun resetForTests(context: Context) {
        synchronized(helperLock) {
            dbHelper?.close()
            dbHelper = null
            val base = context.applicationContext.noBackupFilesDir
            File(base, DB_NAME).delete()
            File(base, DB_NAME + "-journal").delete()
            File(base, DB_NAME + "-wal").delete()
            File(base, DB_NAME + "-shm").delete()
        }
    }
}
''')

    write(root, "V2rayNG/app/src/main/java/com/v2ray/ang/billing/RewardedAdManager.kt", r'''package com.v2ray.ang.billing

import android.app.Activity
import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Handler
import android.os.Looper
import com.v2ray.ang.BuildConfig
import ir.tapsell.mediation.Tapsell
import ir.tapsell.mediation.ad.AdStateListener
import ir.tapsell.mediation.ad.request.RequestResultListener
import ir.tapsell.mediation.ad.show.AdShowCompletionState
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Tapsell Mediation Rewarded Video adapter.
 *
 * Credits require BOTH SDK callbacks: onAdClosed(COMPLETED) and onRewarded().
 * A validated Internet connection is required before requesting and at reward
 * commit; loss of validated connectivity during the ad invalidates the session.
 * This is a client-side check, not proof against a modified APK.
 */
object RewardedAdManager {
    private val activeRequest = AtomicBoolean(false)
    private val mainHandler = Handler(Looper.getMainLooper())

    private fun hasValidatedInternet(context: Context): Boolean {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return false
        val network = manager.activeNetwork ?: return false
        val capabilities = manager.getNetworkCapabilities(network) ?: return false
        return capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
            capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    }

    fun show(
        activity: Activity,
        onGranted: () -> Unit,
        onFailure: (String) -> Unit
    ) {
        if (!activeRequest.compareAndSet(false, true)) {
            onFailure("A rewarded video request is already active")
            return
        }
        if (!hasValidatedInternet(activity)) {
            activeRequest.set(false)
            onFailure("A validated Internet connection is required")
            return
        }

        val zoneId = BuildConfig.TAPSELL_REWARDED_ZONE_ID
        if (zoneId.isBlank()) {
            activeRequest.set(false)
            onFailure("Rewarded ad placement is not configured")
            return
        }

        val requestResolved = AtomicBoolean(false)
        val internetLost = AtomicBoolean(false)
        val watcherClosed = AtomicBoolean(false)
        var failActiveAd: (() -> Unit)? = null
        var networkCallback: ConnectivityManager.NetworkCallback? = null
        var requestTimeout: Runnable? = null
        val connectivity = activity.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

        fun stopWatching() {
            if (!watcherClosed.compareAndSet(false, true)) return
            networkCallback?.let { callback ->
                runCatching { connectivity.unregisterNetworkCallback(callback) }
            }
        }

        fun failPending(message: String) {
            if (!requestResolved.compareAndSet(false, true)) return
            requestTimeout?.let(mainHandler::removeCallbacks)
            stopWatching()
            activeRequest.set(false)
            mainHandler.post { onFailure(message) }
        }

        networkCallback = object : ConnectivityManager.NetworkCallback() {
            private fun reportLoss() {
                internetLost.set(true)
                if (requestResolved.get()) {
                    mainHandler.post {
                        failActiveAd?.invoke()
                    }
                } else {
                    failPending("Internet connectivity was lost during the ad request")
                }
            }

            override fun onLost(network: Network) {
                if (!hasValidatedInternet(activity)) reportLoss()
            }

            override fun onUnavailable() {
                reportLoss()
            }

            override fun onCapabilitiesChanged(
                network: Network,
                networkCapabilities: NetworkCapabilities
            ) {
                if (network == connectivity.activeNetwork &&
                    (!networkCapabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) ||
                     !networkCapabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED))
                ) {
                    reportLoss()
                }
            }
        }

        try {
            val callback = networkCallback ?: throw IllegalStateException("Network callback was not created")
            connectivity.registerDefaultNetworkCallback(callback)
        } catch (error: Exception) {
            activeRequest.set(false)
            onFailure("Could not monitor Internet connectivity")
            return
        }

        requestTimeout = Runnable {
            failPending("No response from the ad service")
        }
        mainHandler.postDelayed(requestTimeout!!, 60_000L)

        try {
            Tapsell.requestRewardedAd(zoneId, object : RequestResultListener {
                override fun onSuccess(adId: String) {
                    if (!requestResolved.compareAndSet(false, true)) return
                    requestTimeout?.let(mainHandler::removeCallbacks)

                    if (internetLost.get() || !hasValidatedInternet(activity)) {
                        stopWatching()
                        activeRequest.set(false)
                        mainHandler.post { onFailure("Internet connectivity is required to complete the ad") }
                        return
                    }
                    if (activity.isFinishing || activity.isDestroyed || adId.isBlank()) {
                        stopWatching()
                        activeRequest.set(false)
                        mainHandler.post { onFailure("The ad could not be shown") }
                        return
                    }

                    val lock = Any()
                    var rewarded = false
                    var closed = false
                    var completionValid = false
                    val terminal = AtomicBoolean(false)
                    var missingRewardTimeout: Runnable? = null
                    var showTimeout: Runnable? = null

                    fun release() {
                        missingRewardTimeout?.let(mainHandler::removeCallbacks)
                        showTimeout?.let(mainHandler::removeCallbacks)
                        stopWatching()
                        activeRequest.set(false)
                    }

                    fun failShow(message: String) {
                        if (!terminal.compareAndSet(false, true)) return
                        release()
                        mainHandler.post {
                            if (!activity.isFinishing && !activity.isDestroyed) onFailure(message)
                        }
                    }
                    failActiveAd = { failShow("Internet connectivity was lost or not validated") }

                    fun commitIfComplete() {
                        if (internetLost.get() || !hasValidatedInternet(activity)) {
                            failShow("A validated Internet connection is required to receive the reward")
                            return
                        }
                        val ready = synchronized(lock) {
                            closed && completionValid && rewarded && terminal.compareAndSet(false, true)
                        }
                        if (!ready) return

                        val granted = runCatching {
                            ConnectionCreditStore.grantReward(activity.applicationContext, adId)
                        }.getOrDefault(false)
                        release()
                        mainHandler.post {
                            if (activity.isFinishing || activity.isDestroyed) return@post
                            if (granted) onGranted()
                            else onFailure("This ad response was already processed")
                        }
                    }

                    showTimeout = Runnable {
                        failShow("The ad did not return a complete reward result")
                    }
                    mainHandler.postDelayed(showTimeout!!, 10 * 60_000L)

                    try {
                        Tapsell.showRewardedAd(adId, activity, object : AdStateListener.Rewarded {
                            override fun onAdImpression() = Unit
                            override fun onAdClicked() = Unit

                            override fun onRewarded() {
                                synchronized(lock) { rewarded = true }
                                commitIfComplete()
                            }

                            override fun onAdClosed(completionState: AdShowCompletionState) {
                                synchronized(lock) {
                                    closed = true
                                    completionValid =
                                        completionState == AdShowCompletionState.COMPLETED
                                }
                                if (completionState != AdShowCompletionState.COMPLETED) {
                                    failShow("The video was not completed")
                                    return
                                }
                                val timeout = Runnable {
                                    failShow("Tapsell reward confirmation was not received")
                                }
                                missingRewardTimeout = timeout
                                mainHandler.postDelayed(timeout, 20_000L)
                                commitIfComplete()
                            }

                            override fun onAdFailed(message: String) {
                                failShow("Tapsell could not show the video")
                            }
                        })
                    } catch (error: Exception) {
                        failShow("Tapsell could not show the video")
                    }
                }

                override fun onFailure(message: String) {
                    failPending("No rewarded video is available")
                }
            })
        } catch (error: Exception) {
            failPending("The ad request failed")
        }
    }
}
''')

    write(root, "V2rayNG/app/src/androidTest/java/com/v2ray/ang/billing/ConnectionCreditStoreTest.kt", r'''package com.v2ray.ang.billing

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors

@RunWith(AndroidJUnit4::class)
class ConnectionCreditStoreTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Before
    fun beforeEach() {
        ConnectionCreditStore.resetForTests(context)
    }

    @After
    fun afterEach() {
        ConnectionCreditStore.resetForTests(context)
    }

    @Test
    fun duplicateRewardIdGrantsOnlyThreeCredits() {
        assertTrue(ConnectionCreditStore.grantReward(context, "ad-response-1"))
        assertFalse(ConnectionCreditStore.grantReward(context, "ad-response-1"))
        assertEquals(3, ConnectionCreditStore.balance(context))
    }

    @Test
    fun threeSuccessfulSessionsConsumeThreeCredits() {
        assertTrue(ConnectionCreditStore.grantReward(context, "ad-response-2"))
        repeat(3) {
            val id = ConnectionCreditStore.reserveConnection(context)
            assertNotNull(id)
            assertEquals(id, ConnectionCreditStore.claimStart(context, id))
            assertTrue(ConnectionCreditStore.isAuthorizedForCore(context, id))
            assertTrue(ConnectionCreditStore.markActive(context, id!!))
            ConnectionCreditStore.finishSession(context, id)
        }
        assertEquals(0, ConnectionCreditStore.balance(context))
        assertNull(ConnectionCreditStore.reserveConnection(context))
    }

    @Test
    fun failedStartRefundsReservedCredit() {
        assertTrue(ConnectionCreditStore.grantReward(context, "ad-response-3"))
        val id = ConnectionCreditStore.reserveConnection(context)
        assertNotNull(id)
        assertNotNull(ConnectionCreditStore.claimStart(context, id))
        ConnectionCreditStore.finishSession(context, id)
        assertEquals(3, ConnectionCreditStore.balance(context))
    }

    @Test
    fun concurrentRequestsCannotSpendSameCreditTwice() {
        assertTrue(ConnectionCreditStore.grantReward(context, "ad-response-4"))
        val gate = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(8)
        try {
            val futures = (1..8).map {
                executor.submit<String?> {
                    gate.await()
                    ConnectionCreditStore.reserveConnection(context)
                }
            }
            gate.countDown()
            assertEquals(1, futures.mapNotNull { it.get() }.size)
            assertEquals(2, ConnectionCreditStore.balance(context))
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun nullSystemIntentCannotCreateNewSession() {
        assertNull(ConnectionCreditStore.claimStart(context, null))
        assertTrue(ConnectionCreditStore.grantReward(context, "ad-response-5"))
        val id = ConnectionCreditStore.reserveConnection(context)
        assertNotNull(id)
        assertNull(ConnectionCreditStore.claimStart(context, null))
        assertEquals(id, ConnectionCreditStore.claimStart(context, id))
    }
}
''')

def patch_config(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/AppConfig.kt"
    value = read(root, rel)
    value = replace_once(
        value,
        '    const val BROADCAST_ACTION_SERVICE = "$ANG_PACKAGE.action.service"\n',
        '    const val EXTRA_CONNECTION_SESSION_ID = "com.nexa.v2rayng.extra.CONNECTION_SESSION_ID"\n'
        '    const val BROADCAST_ACTION_SERVICE = "$ANG_PACKAGE.action.service"\n',
        "AppConfig session extra",
    )
    write(root, rel, value)

    manifest = "V2rayNG/app/src/main/AndroidManifest.xml"
    value = read(root, manifest)
    if 'android:name="com.google.android.gms.permission.AD_ID"' not in value:
        value = replace_once(
            value,
            '    <uses-permission android:name="android.permission.INTERNET" />\n',
            '    <uses-permission android:name="android.permission.INTERNET" />\n'
            '    <uses-permission android:name="com.google.android.gms.permission.AD_ID" />\n',
            "Tapsell AD_ID permission",
        )
    write(root, manifest, value)

    strings = "V2rayNG/app/src/main/res/values/strings.xml"
    value = read(root, strings).replace(
        '<string name="app_name" translatable="false">v2rayNG</string>',
        '<string name="app_name" translatable="false">Nexa</string>',
        1,
    )
    if 'name="reward_ad_required"' not in value:
        value = replace_once(
            value,
            '    <string name="toast_services_failure">Failed to start service</string>\n',
            '    <string name="toast_services_failure">Failed to start service</string>\n'
            '    <string name="reward_ad_required">To connect, watch a complete rewarded video while online.</string>\n'
            '    <string name="reward_ad_granted">Three connection credits added.</string>\n',
            "English rewarded-ad strings",
        )
    write(root, strings, value)

    fa_dir = root / "V2rayNG/app/src/main/res/values-fa"
    fa_dir.mkdir(parents=True, exist_ok=True)
    fa_path = fa_dir / "strings.xml"
    fa_value = fa_path.read_text(encoding="utf-8") if fa_path.exists() else '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n</resources>\n'
    if 'name="reward_ad_required"' not in fa_value:
        fa_value = fa_value.replace(
            "</resources>",
            '    <string name="reward_ad_required">برای اتصال، باید هنگام دسترسی به اینترنت یک ویدیوی جایزه‌دار را کامل تماشا کنید.</string>\n'
            '    <string name="reward_ad_granted">سه اعتبار اتصال اضافه شد.</string>\n</resources>',
        )
    fa_path.write_text(fa_value, encoding="utf-8")

    for rel, package in [
        ("V2rayNG/app/src/main/res/xml/shortcuts.xml", "com.nexa.v2rayng"),
        ("V2rayNG/app/src/fdroid/res/xml/shortcuts.xml", "com.nexa.v2rayng.fdroid"),
    ]:
        path = root / rel
        if path.exists():
            value = path.read_text(encoding="utf-8").replace(
                'android:targetPackage="com.v2ray.ang"',
                'android:targetPackage="' + package + '"',
            )
            path.write_text(value, encoding="utf-8")


def patch_gradle(root: Path) -> None:
    rel = "V2rayNG/app/build.gradle.kts"
    value = read(root, rel)
    value = replace_once(value, 'applicationId = "com.v2ray.ang"', 'applicationId = "com.nexa.v2rayng"', "Nexa application ID")
    value = replace_once(value, 'versionName = "2.4.0"', 'versionName = "2.4.0-nexa1"', "Nexa version name")
    if "TAPSELL_REWARDED_ZONE_ID" not in value:
        value = replace_once(
            value,
            "android {\n",
            'val tapsellAppId = providers.gradleProperty("TAPSELL_APP_ID")\n'
            '    .orElse(providers.environmentVariable("TAPSELL_APP_ID")).getOrElse("")\n'
            'val rewardedZoneId = providers.gradleProperty("TAPSELL_REWARDED_ZONE_ID")\n'
            '    .orElse(providers.environmentVariable("TAPSELL_REWARDED_ZONE_ID")).getOrElse("")\n\n'
            "android {\n",
            "Tapsell Gradle properties",
        )
        value = replace_once(
            value,
            '        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"\n',
            '        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"\n'
            '        manifestPlaceholders["TapsellMediationAppKey"] = tapsellAppId\n'
            '        buildConfigField("String", "TAPSELL_REWARDED_ZONE_ID", "\\"$rewardedZoneId\\"")\n',
            "Tapsell BuildConfig",
        )
    if 'implementation("ir.tapsell:tapsell:' not in value:
        value = replace_once(
            value,
            "dependencies {\n",
            'dependencies {\n'
            '    implementation("ir.tapsell:tapsell:1.4.0-alpha04")\n'
            '    implementation("ir.tapsell.mediation.adapter:legacy:1.4.0-alpha04")\n'
            '    implementation("com.google.guava:guava:31.1-android")\n',
            "Tapsell Mediation dependencies",
        )
    if 'exclude(group = "com.google.guava", module = "listenablefuture")' not in value:
        value = replace_once(
            value,
            "android {\\n",
            'configurations.configureEach {\\n'
            '    exclude(group = "com.google.guava", module = "listenablefuture")\\n'
            '}\\n\\n'
            'android {\\n',
            "Exclude duplicate ListenableFuture stub in favor of Guava",
        )
    if 'androidTestImplementation("androidx.test:core:' not in value:
        value = replace_once(
            value,
            "    androidTestImplementation(libs.androidx.junit)\n",
            "    androidTestImplementation(libs.androidx.junit)\n"
            '    androidTestImplementation("androidx.test:runner:1.6.2")\n'
            '    androidTestImplementation("androidx.test:core:1.6.1")\n',
            "instrumentation dependencies",
        )
    write(root, rel, value)

    rel = "V2rayNG/settings.gradle.kts"
    value = read(root, rel)
    if "https://maven.tapsell.ir" not in value:
        value = replace_once(
            value,
            "    repositories {\n        google()\n",
            '    repositories {\n        maven { url = uri("https://maven.tapsell.ir") }\n        google()\n',
            "Tapsell Maven repository",
        )
    write(root, rel, value)


def patch_service_control(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/contracts/ServiceControl.kt"
    value = read(root, rel)
    value = replace_once(value, "    fun stopService()\n", "    fun stopService(preserveSession: Boolean = false)\n", "ServiceControl stop signature")
    write(root, rel, value)

    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/contracts/Tun2SocksControl.kt"
    value = read(root, rel)
    value = replace_once(value, "    fun startTun2Socks()\n", "    fun startTun2Socks(): Boolean\n", "Tun2Socks result signature")
    write(root, rel, value)


def patch_launcher(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/core/LauncherManager.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.AppConfig\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n",
        "LauncherManager credit-store import",
    )
    value = replace_function(
        value,
        "fun startServiceFromToggle(context: Context): Boolean",
        r'''    fun startServiceFromToggle(context: Context): Boolean {
        if (MmkvManager.getSelectServer().isNullOrEmpty()) {
            context.toast(R.string.app_tile_first_use)
            return false
        }
        val sessionId = ConnectionCreditStore.currentActiveSessionId(context) ?: ConnectionCreditStore.reserveConnection(context)
        if (sessionId.isNullOrBlank()) {
            context.toast(R.string.reward_ad_required)
            return false
        }
        return startAuthorizedService(context, sessionId)
    }''',
        "LauncherManager toggle start",
    )
    value = replace_function(
        value,
        "fun startService(context: Context, guid: String? = null)",
        r'''    fun startService(context: Context, guid: String? = null) {
        if (!guid.isNullOrBlank()) MmkvManager.setSelectServer(guid)
        val sessionId = ConnectionCreditStore.reserveConnection(context)
        if (sessionId.isNullOrBlank()) {
            context.toast(R.string.reward_ad_required)
            return
        }
        startAuthorizedService(context, sessionId, guid)
    }

    fun startServiceOrResumeActive(context: Context, guid: String? = null) {
        if (!guid.isNullOrBlank()) MmkvManager.setSelectServer(guid)
        val sessionId = ConnectionCreditStore.currentActiveSessionId(context)
            ?: ConnectionCreditStore.reserveConnection(context)
        if (sessionId.isNullOrBlank()) {
            context.toast(R.string.reward_ad_required)
            return
        }
        startAuthorizedService(context, sessionId, guid)
    }

    fun startAuthorizedService(
        context: Context,
        sessionId: String,
        guid: String? = null
    ): Boolean {
        return try {
            startContextService(context, guid, sessionId)
            true
        } catch (e: Exception) {
            ConnectionCreditStore.finishSession(context, sessionId)
            LogUtil.e(AppConfig.TAG, "LauncherManager: start failed: " + e.javaClass.simpleName, e)
            context.toast(e.message ?: e.javaClass.simpleName)
            false
        }
    }''',
        "LauncherManager public start",
    )
    value = replace_once(
        value,
        "    private fun startContextService(context: Context) {\n",
        "    private fun startContextService(context: Context, guidOverride: String?, sessionId: String) {\n",
        "LauncherManager internal start signature",
    )
    value = replace_once(
        value,
        "        val guid = MmkvManager.getSelectServer()\n",
        "        if (!guidOverride.isNullOrBlank()) MmkvManager.setSelectServer(guidOverride)\n"
        "        val guid = MmkvManager.getSelectServer()\n",
        "LauncherManager profile override",
    )
    value = replace_once(
        value,
        "        try {\n            ContextCompat.startForegroundService(context, intent)\n",
        "        intent.putExtra(AppConfig.EXTRA_CONNECTION_SESSION_ID, sessionId)\n"
        "        try {\n            ContextCompat.startForegroundService(context, intent)\n",
        "LauncherManager token extra",
    )
    write(root, rel, value)


def patch_main_activity(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/ui/main/MainActivity.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.core.LauncherManager\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n"
        "import com.v2ray.ang.billing.RewardedAdManager\n",
        "MainActivity billing imports",
    )
    value = replace_once(
        value,
        "class MainActivity : HelperBaseComponentActivity() {\n",
        "class MainActivity : HelperBaseComponentActivity() {\n"
        "    private var connectionRequestInProgress = false\n\n",
        "MainActivity in-flight state",
    )
    value = replace_once(
        value,
        """    private val requestVpnPermission =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            if (it.resultCode == RESULT_OK) startV2Ray()
        }
""",
        """    private val requestVpnPermission =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            if (it.resultCode == RESULT_OK) {
                startV2Ray()
            } else {
                connectionRequestInProgress = false
                toast(R.string.reward_ad_required)
            }
        }
""",
        "MainActivity VPN permission callback",
    )
    value = replace_function(
        value,
        "fun requestServiceStart()",
        r'''    private fun requestServiceStart() {
        if (connectionRequestInProgress) return
        if (mainViewModel.uiState.value.selectedGuid.isNullOrEmpty()) {
            toast(R.string.title_file_chooser)
            return
        }

        connectionRequestInProgress = true
        if (ConnectionCreditStore.currentActiveSessionId(this) != null || ConnectionCreditStore.balance(this) > 0) {
            continueConnectionAfterReward()
            return
        }

        RewardedAdManager.show(
            activity = this,
            onGranted = {
                if (isFinishing || isDestroyed) {
                    connectionRequestInProgress = false
                    return@show
                }
                toast(R.string.reward_ad_granted)
                continueConnectionAfterReward()
            },
            onFailure = {
                connectionRequestInProgress = false
                toast(R.string.reward_ad_required)
            }
        )
    }

    private fun continueConnectionAfterReward() {
        if (!SettingsManager.isVpnMode()) {
            startV2Ray()
            return
        }
        val prepareIntent = VpnService.prepare(this)
        if (prepareIntent == null) startV2Ray() else requestVpnPermission.launch(prepareIntent)
    }''',
        "MainActivity rewarded connect flow",
    )
    value = replace_function(
        value,
        "fun startV2Ray()",
        r'''    private fun startV2Ray() {
        if (mainViewModel.uiState.value.selectedGuid.isNullOrEmpty()) {
            connectionRequestInProgress = false
            toast(R.string.title_file_chooser)
            return
        }

        val sessionId = ConnectionCreditStore.currentActiveSessionId(this) ?: ConnectionCreditStore.reserveConnection(this)
        if (sessionId.isNullOrBlank()) {
            connectionRequestInProgress = false
            toast(R.string.reward_ad_required)
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.CINNAMON_BUN) {
            checkAndRequestPermission(PermissionType.ACCESS_LOCAL_NETWORK) {}
        }

        val started = LauncherManager.startAuthorizedService(this, sessionId)
        if (!started) ConnectionCreditStore.finishSession(this, sessionId)
        connectionRequestInProgress = false
    }''',
        "MainActivity authorized start",
    )
    value = replace_once(
        value,
        "        mainViewModel.onAction(MainAction.Initialize)\n",
        "        ConnectionCreditStore.recoverInterruptedSessions(this)\n"
        "        mainViewModel.onAction(MainAction.Initialize)\n",
        "MainActivity ledger recovery",
    )
    write(root, rel, value)


def patch_core_vpn(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/service/CoreVpnService.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.AppConfig\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n",
        "CoreVpnService ledger import",
    )
    value = replace_once(
        value,
        "    private val isStartingLock = AtomicBoolean(false)\n",
        "    private val isStartingLock = AtomicBoolean(false)\n"
        "    private var currentSessionId: String? = null\n",
        "CoreVpnService session field",
    )
    value = replace_function(
        value,
        "fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int",
        r'''    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        NotificationManager.ensureForeground()

        // Android Always-on/sticky starts without our custom token can only resume
        // an existing ACTIVE session. They cannot create a new session or spend no credit.
        val requestedId = intent?.getStringExtra(AppConfig.EXTRA_CONNECTION_SESSION_ID)
        val sessionId = ConnectionCreditStore.claimStart(this, requestedId)
        if (sessionId.isNullOrBlank()) {
            LogUtil.w(AppConfig.TAG, "StartCore-VPN: missing or invalid connection authorization")
            stopSelf(startId)
            return START_NOT_STICKY
        }
        // Do not tear down an already-running tunnel for a duplicate or conflicting
        // start command. The prior session ID is checked before it is overwritten.
        if (CoreServiceManager.isRunning()) {
            val runningSessionId = currentSessionId ?: ConnectionCreditStore.currentActiveSessionId(this)
            if (runningSessionId == sessionId) {
                currentSessionId = sessionId
                return START_STICKY
            }
            ConnectionCreditStore.finishSession(this, sessionId)
            LogUtil.w(AppConfig.TAG, "StartCore-VPN: rejected a second session while core is running")
            return START_NOT_STICKY
        }
        currentSessionId = sessionId

        val isSystemVpnStart = intent == null || intent.action == SERVICE_INTERFACE
        if (isSystemVpnStart) unlockStart()
        if (!tryLockStart()) {
            LogUtil.w(AppConfig.TAG, "StartCore-VPN: start already in progress")
            return START_NOT_STICKY
        }
        if (!setupVpnService()) {
            ConnectionCreditStore.finishSession(this, currentSessionId)
            currentSessionId = null
            unlockStart()
            stopSelf(startId)
            return START_NOT_STICKY
        }
        startService()
        return START_STICKY
    }''',
        "CoreVpnService start command gate",
    )
    value = replace_function(
        value,
        "fun startService()",
        r'''    override fun startService() {
        if (!::mInterface.isInitialized) {
            LogUtil.e(AppConfig.TAG, "StartCore-VPN: VPN interface was not initialized")
            ConnectionCreditStore.finishSession(this, currentSessionId)
            currentSessionId = null
            stopAllService()
            return
        }
        val sessionId = currentSessionId
        if (sessionId.isNullOrBlank() || !CoreServiceManager.startCoreLoop(mInterface, sessionId)) {
            LogUtil.e(AppConfig.TAG, "StartCore-VPN: authorized core start failed")
            stopAllService()
            return
        }
        if (!ConnectionCreditStore.markActive(this, sessionId)) {
            LogUtil.e(AppConfig.TAG, "StartCore-VPN: could not activate connection session")
            stopAllService()
            return
        }
        RootLanSharing.startClientSharing(this)
    }''',
        "CoreVpnService core start",
    )
    value = replace_function(
        value,
        "fun stopService()",
        r'''    override fun stopService(preserveSession: Boolean) {
        stopAllService(true, preserveSession)
    }''',
        "CoreVpnService stop callback",
    )
    value = replace_once(
        value,
        "        runTun2socks()\n        return true\n",
        "        if (!runTun2socks()) {\n"
        "            LogUtil.e(AppConfig.TAG, \"StartCore-VPN: tun2socks failed to start\")\n"
        "            stopAllService()\n"
        "            return false\n"
        "        }\n"
        "        return true\n",
        "CoreVpnService tun2socks result",
    )
    value = replace_function(
        value,
        "fun runTun2socks()",
        r'''    private fun runTun2socks(): Boolean {
        if (SettingsManager.isUsingHevTun()) {
            tun2SocksService = TProxyService(
                context = applicationContext,
                vpnInterface = mInterface,
                isRunningProvider = { isRunning },
                restartCallback = { runTun2socks() }
            )
        } else {
            tun2SocksService = null
        }
        return tun2SocksService?.startTun2Socks() ?: true
    }''',
        "CoreVpnService tun2socks helper",
    )
    value = replace_once(
        value,
        "    private fun stopAllService(isForced: Boolean = true) {\n",
        "    private fun stopAllService(isForced: Boolean = true, preserveSession: Boolean = false) {\n"
        "        if (preserveSession) {\n"
        "            currentSessionId = null\n"
        "        } else {\n"
        "            ConnectionCreditStore.finishSession(this, currentSessionId)\n"
        "            currentSessionId = null\n"
        "        }\n",
        "CoreVpnService session close",
    )
    value = replace_once(
        value,
        "    override fun onDestroy() {\n        super.onDestroy()\n",
        "    override fun onDestroy() {\n"
        "        ConnectionCreditStore.finishSession(this, currentSessionId)\n"
        "        currentSessionId = null\n"
        "        super.onDestroy()\n",
        "CoreVpnService destroy cleanup",
    )
    write(root, rel, value)


def patch_tun2socks(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/service/TProxyService.kt"
    value = read(root, rel)
    value = replace_function(
        value,
        "fun startTun2Socks()",
        r'''    override fun startTun2Socks(): Boolean {
        val configContent = buildConfig()
        val configFile = File(context.filesDir, "hev-socks5-tunnel.yaml").apply {
            writeText(configContent)
        }
        return try {
            TProxyStartService(configFile.absolutePath, vpnInterface.fd)
        } catch (e: Exception) {
            LogUtil.e(AppConfig.TAG, "HevSocks5Tunnel failed to start: " + e.javaClass.simpleName, e)
            false
        }
    }''',
        "TProxyService checked native start",
    )
    write(root, rel, value)


def patch_proxy_service(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/service/CoreProxyOnlyService.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.AppConfig\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n",
        "CoreProxyOnlyService ledger import",
    )
    value = replace_once(
        value,
        "class CoreProxyOnlyService : Service(), ServiceControl {\n",
        "class CoreProxyOnlyService : Service(), ServiceControl {\n"
        "    private var currentSessionId: String? = null\n",
        "CoreProxyOnlyService session field",
    )
    value = replace_function(
        value,
        "fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int",
        r'''    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        NotificationManager.ensureForeground()
        val requestedId = intent?.getStringExtra(AppConfig.EXTRA_CONNECTION_SESSION_ID)
        val sessionId = ConnectionCreditStore.claimStart(this, requestedId)
        if (sessionId.isNullOrBlank()) {
            LogUtil.w(AppConfig.TAG, "StartCore-Proxy: missing connection authorization")
            stopSelf(startId)
            return START_NOT_STICKY
        }
        if (CoreServiceManager.isRunning()) {
            val runningSessionId = currentSessionId ?: ConnectionCreditStore.currentActiveSessionId(this)
            if (runningSessionId == sessionId) {
                currentSessionId = sessionId
                return START_STICKY
            }
            ConnectionCreditStore.finishSession(this, sessionId)
            LogUtil.w(AppConfig.TAG, "Rejected a second session while core is running")
            return START_NOT_STICKY
        }
        currentSessionId = sessionId

        if (!CoreServiceManager.startCoreLoop(null, sessionId)) {
            ConnectionCreditStore.finishSession(this, sessionId)
            currentSessionId = null
            stopSelf(startId)
            return START_NOT_STICKY
        }
        if (!ConnectionCreditStore.markActive(this, sessionId)) {
            ConnectionCreditStore.finishSession(this, sessionId)
            currentSessionId = null
            stopSelf(startId)
            return START_NOT_STICKY
        }
        return START_STICKY
    }''',
        "CoreProxyOnlyService start gate",
    )
    value = replace_function(
        value,
        "fun stopService()",
        r'''    override fun stopService(preserveSession: Boolean) {
        if (!preserveSession) ConnectionCreditStore.finishSession(this, currentSessionId)
        currentSessionId = null
        stopSelf()
    }''',
        "CoreProxyOnlyService stop gate",
    )
    value = replace_once(
        value,
        "    override fun onDestroy() {\n        super.onDestroy()\n",
        "    override fun onDestroy() {\n"
        "        ConnectionCreditStore.finishSession(this, currentSessionId)\n"
        "        currentSessionId = null\n"
        "        super.onDestroy()\n",
        "CoreProxyOnlyService destroy cleanup",
    )
    write(root, rel, value)


def patch_root_service(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/service/CoreRootService.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.AppConfig\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n",
        "CoreRootService ledger import",
    )
    value = replace_once(
        value,
        "    private var setupJob: Job? = null\n",
        "    private var setupJob: Job? = null\n"
        "    private var currentSessionId: String? = null\n",
        "CoreRootService session field",
    )
    value = replace_function(
        value,
        "fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int",
        r'''    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        NotificationManager.ensureForeground()
        val requestedId = intent?.getStringExtra(AppConfig.EXTRA_CONNECTION_SESSION_ID)
        val sessionId = ConnectionCreditStore.claimStart(this, requestedId)
        if (sessionId.isNullOrBlank()) {
            LogUtil.w(AppConfig.TAG, "StartCore-Root: missing connection authorization")
            stopSelf(startId)
            return START_NOT_STICKY
        }
        if (CoreServiceManager.isRunning()) {
            val runningSessionId = currentSessionId ?: ConnectionCreditStore.currentActiveSessionId(this)
            if (runningSessionId == sessionId) {
                currentSessionId = sessionId
                return START_STICKY
            }
            ConnectionCreditStore.finishSession(this, sessionId)
            LogUtil.w(AppConfig.TAG, "Rejected a second session while core is running")
            return START_NOT_STICKY
        }
        currentSessionId = sessionId

        if (!CoreServiceManager.startCoreLoop(null, sessionId)) {
            ConnectionCreditStore.finishSession(this, sessionId)
            currentSessionId = null
            stopSelf(startId)
            return START_NOT_STICKY
        }

        setupJob = CoroutineScope(Dispatchers.IO).launch {
            if (RootProxyManager.start(this@CoreRootService)) {
                if (!ConnectionCreditStore.markActive(this@CoreRootService, sessionId)) {
                    LogUtil.e(AppConfig.TAG, "StartCore-Root: failed to activate session")
                    stopService()
                }
            } else {
                LogUtil.e(AppConfig.TAG, "StartCore-Root: root routing setup failed")
                stopService()
            }
        }
        return START_STICKY
    }''',
        "CoreRootService start gate",
    )
    value = replace_function(
        value,
        "fun stopService()",
        r'''    override fun stopService(preserveSession: Boolean) {
        if (!preserveSession) ConnectionCreditStore.finishSession(this, currentSessionId)
        currentSessionId = null
        stopSelf()
    }''',
        "CoreRootService stop gate",
    )
    value = replace_once(
        value,
        "    override fun onDestroy() {\n        super.onDestroy()\n",
        "    override fun onDestroy() {\n"
        "        ConnectionCreditStore.finishSession(this, currentSessionId)\n"
        "        currentSessionId = null\n"
        "        super.onDestroy()\n",
        "CoreRootService destroy cleanup",
    )
    write(root, rel, value)


def patch_boot_receiver(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/receiver/BootReceiver.kt"
    value = read(root, rel)
    value = replace_once(
        value,
        "        LauncherManager.startService(context)\n",
        "        LauncherManager.startServiceOrResumeActive(context)\n",
        "BootReceiver session resume",
    )
    write(root, rel, value)


def patch_core_manager(root: Path) -> None:
    rel = "V2rayNG/app/src/main/java/com/v2ray/ang/core/CoreServiceManager.kt"
    value = read(root, rel)
    value = add_import(
        value,
        "import com.v2ray.ang.AppConfig\n",
        "import com.v2ray.ang.billing.ConnectionCreditStore\n",
        "CoreServiceManager ledger import",
    )
    value = replace_once(
        value,
        "    fun startCoreLoop(vpnInterface: ParcelFileDescriptor?): Boolean {\n",
        "    fun startCoreLoop(vpnInterface: ParcelFileDescriptor?, sessionId: String): Boolean {\n",
        "CoreServiceManager start signature",
    )
    value = replace_once(
        value,
        '        if (service == null) {\n            LogUtil.e(AppConfig.TAG, "StartCore-Manager: Service is null")\n            return false\n        }\n\n        try {\n',
        '        if (service == null) {\n            LogUtil.e(AppConfig.TAG, "StartCore-Manager: Service is null")\n            return false\n        }\n        if (!ConnectionCreditStore.isAuthorizedForCore(service, sessionId)) {\n            LogUtil.e(AppConfig.TAG, "StartCore-Manager: connection authorization rejected")\n            return false\n        }\n\n        try {\n',
        "CoreServiceManager second authorization gate",
    )
    value = replace_once(
        value,
        """                    val pendingResult = goAsync()
                    CoroutineScope(Dispatchers.Default).launch {
                        try {
                            serviceControl.stopService()
                            delay(500L)
                            LauncherManager.startService(serviceControl.getService())
                        } finally {
                            pendingResult.finish()
                        }
                    }
""",
        """                    val pendingResult = goAsync()
                    CoroutineScope(Dispatchers.Default).launch {
                        val service = serviceControl.getService()
                        val sessionId = ConnectionCreditStore.currentActiveSessionId(service)
                        try {
                            if (!sessionId.isNullOrBlank()) {
                                serviceControl.stopService(preserveSession = true)
                                delay(500L)
                                if (!LauncherManager.startAuthorizedService(service, sessionId)) {
                                    ConnectionCreditStore.finishSession(service, sessionId)
                                }
                            } else {
                                serviceControl.stopService()
                            }
                        } finally {
                            pendingResult.finish()
                        }
                    }
""",
        "CoreServiceManager notification restart",
    )
    write(root, rel, value)


def patch_all(root: Path) -> None:
    if not (root / ".gitmodules").is_file():
        raise RuntimeError("This patch expects the complete upstream repository with submodules")
    patch_config(root)
    patch_gradle(root)
    patch_credit_store(root)
    patch_service_control(root)
    patch_launcher(root)
    patch_main_activity(root)
    patch_tun2socks(root)
    patch_core_vpn(root)
    patch_proxy_service(root)
    patch_root_service(root)
    patch_core_manager(root)
    patch_boot_receiver(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    patch_all(root)
    print("Applied Nexa rewarded-connect patch to " + str(root))
    print("Application ID: com.nexa.v2rayng")
    print("Tapsell SDK: Mediation 1.4.0-alpha04 (official sample version)")
    print("TAPSELL_APP_ID and TAPSELL_REWARDED_ZONE_ID are read from build properties/environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
