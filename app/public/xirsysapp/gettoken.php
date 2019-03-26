<?php
    // how to install PHP on Ubuntu
    // sudo apt-get install php libapache2-mod-php
    // how to enable curl in PHP
    // sudo apt-get install php-curl
    // how curl works in PHP
    // http://php.net/manual/en/function.curl-exec.php
    $curl = curl_init();
    curl_setopt_array( $curl, array (
        CURLOPT_URL => "https://global.xirsys.net/_token/".$_POST["channel"]."?k=".$_POST["username"],
        CURLOPT_USERPWD => "ggsato:14f79f12-4161-11e9-b010-0242ac110003",
        CURLOPT_HTTPAUTH => CURLAUTH_BASIC,
        CURLOPT_CUSTOMREQUEST => "PUT",
        CURLOPT_RETURNTRANSFER => 1
    ));
    $resp = curl_exec($curl);
    print $resp;
    curl_close($curl);
?>