package com.kyawmt2000.courierrider

import android.app.Activity
import android.content.Intent
import com.google.android.gms.auth.api.signin.GoogleSignIn
import com.google.android.gms.auth.api.signin.GoogleSignInOptions
import com.google.android.gms.auth.api.signin.GoogleSignInStatusCodes
import com.google.android.gms.common.api.ApiException

object LegacyGoogleAccountSignIn {
    fun signInIntent(activity: Activity): Intent {
        val options = GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
            .requestEmail()
            .requestProfile()
            .requestIdToken(AppConfig.googleOAuthWebClientId)
            .build()
        return GoogleSignIn.getClient(activity, options).signInIntent
    }

    fun resultFromIntent(data: Intent?): GoogleAccountSignInResult {
        val account = try {
            GoogleSignIn.getSignedInAccountFromIntent(data)
                .getResult(ApiException::class.java)
        } catch (error: ApiException) {
            throw IllegalStateException("Google sign-in failed: ${GoogleSignInStatusCodes.getStatusCodeString(error.statusCode)} (${error.statusCode})")
        }
        val idToken = account.idToken ?: throw IllegalStateException("Google sign-in did not return an ID token.")
        return GoogleAccountSignInResult(
            idToken = idToken,
            email = account.email,
            name = account.displayName,
            subject = account.id
        )
    }
}
