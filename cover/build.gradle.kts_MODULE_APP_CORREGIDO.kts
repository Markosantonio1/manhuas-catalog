import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

/*
 * ============================================================
 * FIRMA DE LA APLICACIÓN
 * ============================================================
 */

val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties()

if (!keystorePropertiesFile.exists()) {
    throw GradleException(
        "No se encontró keystore.properties en la raíz del proyecto."
    )
}

keystorePropertiesFile.inputStream().use {
    keystoreProperties.load(it)
}

android {
    namespace = "com.manhuas.app"

    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "com.manhuas.app"
        minSdk = 24
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    /*
     * ========================================================
     * CONFIGURACIÓN DE FIRMA
     * ========================================================
     */

    signingConfigs {
        create("release") {
            val storeFileValue = keystoreProperties.getProperty("storeFile")
            val storePasswordValue = keystoreProperties.getProperty("storePassword")
            val keyAliasValue = keystoreProperties.getProperty("keyAlias")
            val keyPasswordValue = keystoreProperties.getProperty("keyPassword")

            if (
                storeFileValue.isNullOrBlank() ||
                storePasswordValue.isNullOrBlank() ||
                keyAliasValue.isNullOrBlank() ||
                keyPasswordValue.isNullOrBlank()
            ) {
                throw GradleException(
                    "Faltan datos en keystore.properties."
                )
            }

            storeFile = rootProject.file(storeFileValue)
            storePassword = storePasswordValue
            keyAlias = keyAliasValue
            keyPassword = keyPasswordValue
        }
    }

    /*
     * ========================================================
     * BUILD TYPES
     * ========================================================
     */

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")

            optimization {
                enable = false
            }
        }
    }

    /*
     * ========================================================
     * JAVA / KOTLIN
     * ========================================================
     */

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    /*
     * ========================================================
     * COMPOSE
     * ========================================================
     */

    buildFeatures {
        compose = true
    }
}

/*
 * ============================================================
 * DEPENDENCIAS
 * ============================================================
 */

dependencies {
    implementation(
        platform(libs.androidx.compose.bom)
    )

    implementation(
        libs.androidx.activity.compose
    )

    implementation(
        libs.androidx.compose.material3
    )

    implementation(
        libs.androidx.compose.ui
    )

    implementation(
        libs.androidx.compose.ui.graphics
    )

    implementation(
        libs.androidx.compose.ui.tooling.preview
    )

    implementation(
        libs.androidx.compose.foundation
    )

    implementation(
        libs.androidx.core.ktx
    )

    implementation(
        libs.androidx.lifecycle.runtime.ktx
    )

    testImplementation(
        libs.junit
    )

    androidTestImplementation(
        platform(libs.androidx.compose.bom)
    )

    androidTestImplementation(
        libs.androidx.compose.ui.test.junit4
    )

    androidTestImplementation(
        libs.androidx.espresso.core
    )

    androidTestImplementation(
        libs.androidx.junit
    )

    debugImplementation(
        libs.androidx.compose.ui.test.manifest
    )

    debugImplementation(
        libs.androidx.compose.ui.tooling
    )
}
