/* Ключ устройства для подписи (WebAuthn; spec 0077 §4, ADR-008 §8): общие помощники
 * страниц кабинета — window.NecturnPasskey.
 *
 * Сервер отдаёт параметры церемонии JSON-строкой (`options_to_json` библиотеки
 * webauthn, байты — base64url) и ждёт ответ ключа тоже JSON-строкой. Встроенные
 * PublicKeyCredential.parseCreationOptionsFromJSON и credential.toJSON есть не во
 * всех браузерах телефонов пилота, поэтому перевод — здесь, руками:
 *   create(optionsJson) → JSON ответа navigator.credentials.create (привязка);
 *   get(optionsJson)    → JSON ответа navigator.credentials.get (подтверждение, PR 4б-2);
 *   available()         → Promise<bool>: есть ли встроенный ключ с проверкой владельца.
 * Отказ владельца или браузера — исключение; что показать человеку, решает страница.
 */
(function () {
  "use strict";

  function fromBase64url(text) {
    var base64 = text.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) base64 += "=";
    var binary = window.atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  function toBase64url(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function descriptors(list) {
    return (list || []).map(function (item) {
      return { type: item.type, id: fromBase64url(item.id), transports: item.transports };
    });
  }

  async function available() {
    if (!window.PublicKeyCredential || !navigator.credentials) return false;
    var check = window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable;
    if (typeof check !== "function") return false;
    try {
      return await check.call(window.PublicKeyCredential);
    } catch (error) {
      return false;
    }
  }

  async function create(optionsJson) {
    var options = JSON.parse(optionsJson);
    var publicKey = Object.assign({}, options, {
      challenge: fromBase64url(options.challenge),
      user: Object.assign({}, options.user, { id: fromBase64url(options.user.id) }),
      excludeCredentials: descriptors(options.excludeCredentials)
    });
    var credential = await navigator.credentials.create({ publicKey: publicKey });
    var response = credential.response;
    var transports = typeof response.getTransports === "function" ? response.getTransports() : [];
    return JSON.stringify({
      id: credential.id,
      rawId: toBase64url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: {
        clientDataJSON: toBase64url(response.clientDataJSON),
        attestationObject: toBase64url(response.attestationObject),
        transports: transports
      }
    });
  }

  async function get(optionsJson) {
    var options = JSON.parse(optionsJson);
    var publicKey = Object.assign({}, options, {
      challenge: fromBase64url(options.challenge),
      allowCredentials: descriptors(options.allowCredentials)
    });
    var credential = await navigator.credentials.get({ publicKey: publicKey });
    var response = credential.response;
    return JSON.stringify({
      id: credential.id,
      rawId: toBase64url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: {
        clientDataJSON: toBase64url(response.clientDataJSON),
        authenticatorData: toBase64url(response.authenticatorData),
        signature: toBase64url(response.signature),
        userHandle: response.userHandle ? toBase64url(response.userHandle) : undefined
      }
    });
  }

  window.NecturnPasskey = { available: available, create: create, get: get };
})();
