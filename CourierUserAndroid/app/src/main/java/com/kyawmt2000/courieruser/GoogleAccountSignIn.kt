package com.kyawmt2000.courieruser

import android.app.Activity
import androidx.credentials.CredentialManager
import androidx.credentials.CustomCredential
import androidx.credentials.GetCredentialRequest
import com.google.android.libraries.identity.googleid.GetGoogleIdOption
import com.google.android.libraries.identity.googleid.GetSignInWithGoogleOption
import com.google.android.libraries.identity.googleid.GoogleIdTokenCredential
import kotlinx.coroutines.CancellationException

data class GoogleAccountSignInResult(
    val idToken: String,
    val email: String?,
    val name: String?,
    val subject: String?
)

object GoogleAccountSignIn {
    suspend fun signIn(activity: Activity): GoogleAccountSignInResult {
        if (!AppConfig.isGoogleSignInConfigured) {
            throw IllegalStateException("请先在 AppConfig.kt 填入 Blink Express 的 Web Google Client ID")
        }

        return try {
            requestGoogleAccount(activity, filterAuthorizedAccounts = true)
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            try {
                requestGoogleAccount(activity, filterAuthorizedAccounts = false)
            } catch (fallbackError: Exception) {
                if (fallbackError is CancellationException) throw fallbackError
                requestSignInWithGoogle(activity)
            }
        }
    }

    private suspend fun requestGoogleAccount(
        activity: Activity,
        filterAuthorizedAccounts: Boolean
    ): GoogleAccountSignInResult {
        val googleIdOption = GetGoogleIdOption.Builder()
            .setFilterByAuthorizedAccounts(filterAuthorizedAccounts)
            .setServerClientId(AppConfig.googleOAuthWebClientId)
            .setAutoSelectEnabled(filterAuthorizedAccounts)
            .build()
        val request = GetCredentialRequest.Builder()
            .addCredentialOption(googleIdOption)
            .build()
        val result = CredentialManager.create(activity).getCredential(
            context = activity,
            request = request
        )
        val credential = result.credential
        if (
            credential is CustomCredential &&
            credential.type == GoogleIdTokenCredential.TYPE_GOOGLE_ID_TOKEN_CREDENTIAL
        ) {
            val googleCredential = GoogleIdTokenCredential.createFrom(credential.data)
            return GoogleAccountSignInResult(
                idToken = googleCredential.idToken,
                email = googleCredential.id,
                name = googleCredential.displayName,
                subject = googleCredential.id
            )
        }
        throw IllegalStateException("Google 登录没有返回有效账号")
    }

    private suspend fun requestSignInWithGoogle(activity: Activity): GoogleAccountSignInResult {
        val googleOption = GetSignInWithGoogleOption.Builder(AppConfig.googleOAuthWebClientId)
            .build()
        val request = GetCredentialRequest.Builder()
            .addCredentialOption(googleOption)
            .build()
        val result = CredentialManager.create(activity).getCredential(
            context = activity,
            request = request
        )
        val credential = result.credential
        if (
            credential is CustomCredential &&
            credential.type == GoogleIdTokenCredential.TYPE_GOOGLE_ID_TOKEN_CREDENTIAL
        ) {
            val googleCredential = GoogleIdTokenCredential.createFrom(credential.data)
            return GoogleAccountSignInResult(
                idToken = googleCredential.idToken,
                email = googleCredential.id,
                name = googleCredential.displayName,
                subject = googleCredential.id
            )
        }
        throw IllegalStateException("Google 登录没有返回有效账号")
    }
}
