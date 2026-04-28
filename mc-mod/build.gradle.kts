plugins {
    id("fabric-loom") version "1.10.5"
    kotlin("jvm") version "2.3.21"
}

val minecraftVersion: String by project
val yarnMappings: String by project
val loaderVersion: String by project
val modVersion: String by project
val mavenGroup: String by project
val archivesBaseName: String by project
val fabricVersion: String by project
val fabricKotlinVersion: String by project

base {
    archivesName.set(archivesBaseName)
}

version = modVersion
group = mavenGroup

repositories {
    maven("https://maven.fabricmc.net/") { name = "Fabric" }
    maven("https://impactdevelopment.github.io/maven/") { name = "Baritone" }
    mavenCentral()
}

dependencies {
    minecraft("com.mojang:minecraft:$minecraftVersion")
    mappings("net.fabricmc:yarn:$yarnMappings:v2")
    modImplementation("net.fabricmc:fabric-loader:$loaderVersion")
    modImplementation("net.fabricmc.fabric-api:fabric-api:$fabricVersion")
    modImplementation("net.fabricmc:fabric-language-kotlin:$fabricKotlinVersion")
    // Phase 6 — events WebSocket. `include` ships Java-WebSocket inside
    // the mod JAR (Loom's jar-in-jar) so Fabric resolves it at runtime
    // without a separate dep on the user's classpath.
    include(implementation("org.java-websocket:Java-WebSocket:1.5.7")!!)
}

tasks.withType<JavaCompile> {
    options.encoding = "UTF-8"
    options.release.set(21)
}

kotlin {
    jvmToolchain(21)
}

tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile> {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_21)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
    withSourcesJar()
}

tasks.named<ProcessResources>("processResources") {
    inputs.property("version", project.version)
    inputs.property("loader_version", loaderVersion)
    inputs.property("minecraft_version", minecraftVersion)
    filesMatching("fabric.mod.json") {
        expand(
            mapOf(
                "version" to project.version,
                "loader_version" to loaderVersion,
                "minecraft_version" to minecraftVersion,
            )
        )
    }
}

tasks.named<Jar>("jar") {
    from("LICENSE") {
        rename { "${it}_${archivesBaseName}" }
    }
}
