function isCredentialPath(value) {
  if (typeof value !== 'string') return true;
  return value.split(/[\\/]/).some(part => /^(?:\.env(?:\..*)?|\.ssh|\.aws|\.gnupg|\.azure|\.kube|\.git|\.netrc|\.npmrc|\.pypirc|\.git-credentials|credentials(?:\..*)?|secrets?(?:\..*)?|tokens?(?:\..*)?|passwords?(?:\..*)?|cookies?(?:\..*)?|login data|id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?)$/i.test(part) || /\.(?:pem|key|p12|pfx|jks|keystore|keychain|keychain-db)$/i.test(part));
}
module.exports = {isCredentialPath};
